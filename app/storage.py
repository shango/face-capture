"""Object storage abstraction.

Two implementations satisfy the `Storage` protocol:

  - `LocalStorage`  — writes under `settings.local_storage_dir`. Download URLs
                      are HMAC-signed and served by the FastAPI app itself via
                      `make_local_storage_router`. Intended for dev only.
  - `R2Storage`     — Cloudflare R2 (S3-compatible) via boto3. Download URLs
                      are S3 pre-signed URLs.

Both backends are async at the call site; blocking work (file IO, boto3) is
delegated to the default thread pool. Routes and the worker should obtain an
instance from `get_storage()`, which caches the selected backend per process.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .config import Settings, get_settings


class StorageError(Exception):
    """Raised for any storage-layer failure."""


class StorageKeyNotFound(StorageError):
    """Raised when an operation targets a key that does not exist."""


class Storage(Protocol):
    """Object storage interface.

    Keys are POSIX-style relative paths (e.g. `sources/<job-id>.mp4`).
    Implementations must reject absolute paths and parent-directory escapes.
    """

    async def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
    ) -> None: ...

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None: ...

    async def get_file(self, key: str, dest: Path) -> None: ...

    def signed_download_url(self, key: str, *, expires_in: int = 3600) -> str: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


def _validate_key(key: str) -> PurePosixPath:
    """Normalize and reject unsafe keys."""
    if not key:
        raise StorageError("Storage key must not be empty.")
    if key.startswith("/"):
        raise StorageError(f"Storage key must be relative: {key!r}")
    pure = PurePosixPath(key)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise StorageError(f"Invalid storage key: {key!r}")
    return pure


# ---------------------------------------------------------------------------
# LocalStorage
# ---------------------------------------------------------------------------


class LocalStorage:
    """Filesystem-backed storage for development.

    Signed URL scheme: `/storage/<key>?exp=<unix_ts>&sig=<urlsafe-b64 hmac>`.
    HMAC-SHA256 over `key:exp` with a per-process random secret. URLs do not
    survive a restart — fine for dev where the worker is in-process and
    completion is fast.
    """

    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.local_storage_dir).resolve()
        self._secret = secrets.token_bytes(32)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        _validate_key(key)
        full = (self._root / key).resolve()
        if not full.is_relative_to(self._root):
            raise StorageError(f"Key escapes storage root: {key!r}")
        return full

    def _sign(self, key: str, exp: int) -> str:
        msg = f"{key}:{exp}".encode("utf-8")
        digest = hmac.new(self._secret, msg, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def verify_signature(self, key: str, exp: int, sig: str) -> bool:
        if exp < int(time.time()):
            return False
        try:
            expected = self._sign(key, exp)
        except Exception:
            return False
        return hmac.compare_digest(expected, sig)

    async def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
    ) -> None:
        del content_type  # not stored for local backend
        dest = self._resolve(key)

        def _write() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fp:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)

        await asyncio.to_thread(_write)

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        del content_type
        dest = self._resolve(key)

        def _copy() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as src, dest.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

        await asyncio.to_thread(_copy)

    async def get_file(self, key: str, dest: Path) -> None:
        src = self._resolve(key)

        def _copy() -> None:
            if not src.is_file():
                raise StorageKeyNotFound(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as fp, dest.open("wb") as out:
                while True:
                    chunk = fp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

        await asyncio.to_thread(_copy)

    def signed_download_url(self, key: str, *, expires_in: int = 3600) -> str:
        _validate_key(key)
        exp = int(time.time()) + expires_in
        sig = self._sign(key, exp)
        return f"/storage/{key}?exp={exp}&sig={sig}"

    async def delete(self, key: str) -> None:
        target = self._resolve(key)

        def _unlink() -> None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_unlink)

    async def exists(self, key: str) -> bool:
        target = self._resolve(key)
        return await asyncio.to_thread(target.is_file)


# ---------------------------------------------------------------------------
# R2Storage
# ---------------------------------------------------------------------------


class R2Storage:
    """Cloudflare R2 backend using boto3's S3 client.

    All boto3 calls are sync; we wrap them with `asyncio.to_thread` so the
    event loop stays responsive. The client is thread-safe per botocore docs.
    """

    def __init__(self, settings: Settings) -> None:
        missing = [
            name
            for name, value in (
                ("R2_ENDPOINT_URL", settings.r2_endpoint_url),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
                ("R2_BUCKET", settings.r2_bucket),
            )
            if not value
        ]
        if missing:
            raise StorageError(
                "R2Storage selected but missing settings: " + ", ".join(missing)
            )

        self._bucket: str = settings.r2_bucket  # type: ignore[assignment]
        # Force S3v4 signing — required by R2 — and disable boto's checksum
        # header injection that R2 rejects on PUT.
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name=settings.r2_region,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    async def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
    ) -> None:
        _validate_key(key)
        await asyncio.to_thread(
            self._client.upload_fileobj,
            source,
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        _validate_key(key)
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    async def get_file(self, key: str, dest: Path) -> None:
        _validate_key(key)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _download() -> None:
            try:
                self._client.download_file(self._bucket, key, str(dest))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("404", "NoSuchKey"):
                    raise StorageKeyNotFound(key) from exc
                raise

        await asyncio.to_thread(_download)

    def signed_download_url(self, key: str, *, expires_in: int = 3600) -> str:
        _validate_key(key)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    async def delete(self, key: str) -> None:
        _validate_key(key)
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
        )

    async def exists(self, key: str) -> bool:
        _validate_key(key)

        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

        return await asyncio.to_thread(_head)


# ---------------------------------------------------------------------------
# Factory & FastAPI integration
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings)
    return R2Storage(settings)


def make_local_storage_router(storage: LocalStorage) -> APIRouter:
    """Router that serves HMAC-signed download URLs for LocalStorage.

    Only mount when `settings.storage_backend == "local"`. With R2, signed
    URLs point at Cloudflare directly and this router is unnecessary.
    """
    router = APIRouter()

    @router.get("/storage/{key:path}", include_in_schema=False)
    async def serve_local(
        key: str,
        exp: int = Query(...),
        sig: str = Query(...),
    ) -> FileResponse:
        if not storage.verify_signature(key, exp, sig):
            raise HTTPException(status_code=403, detail="Invalid or expired signature.")
        try:
            path = storage._resolve(key)
        except StorageError:
            raise HTTPException(status_code=400, detail="Invalid key.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        return FileResponse(path)

    return router
