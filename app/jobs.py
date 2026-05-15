"""Job HTTP endpoints — in-memory state, no database.

Routes:
  POST  /api/jobs               multipart upload, returns {id, status, created_at}
  GET   /api/jobs/{id}          current status snapshot
  GET   /api/jobs/{id}/bundle   signed URL for the completed bundle

Job state lives in a module-level dict. State is lost on process restart;
this app is one-off-per-video with no accounts, so re-upload is the recovery
path. R2 lifecycle rules handle bundle expiry.

The pipeline runs in a ProcessPoolExecutor so MediaPipe's CPU work does not
block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, BinaryIO

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel

from .config import Settings, get_settings
from .storage import Storage, get_storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------


class JobStatus:
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass
class JobRecord:
    id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_log: str | None = None
    bundle_key: str | None = None
    source_duration_seconds: float | None = None
    # Sanitised stem of the uploaded filename; names the bundle/files.
    clip_stem: str = "clip"


_jobs: dict[uuid.UUID, JobRecord] = {}
_executor: ProcessPoolExecutor | None = None


def get_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=1)
    return _executor


def shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class JobCreated(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime


class JobDetail(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_log: str | None
    source_duration_seconds: float | None


class BundleDownload(BaseModel):
    url: str
    expires_in: int


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


ALLOWED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
DOWNLOAD_URL_TTL_SECONDS = 3600
_CONTENT_LENGTH_HEADROOM = 1 * 1024 * 1024
_ERROR_LOG_MAX = 8000


class _UploadTooLarge(Exception):
    pass


class _BoundedReader:
    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._limit = limit
        self._read = 0
        # boto3's upload_fileobj catches exceptions from read() and may
        # re-wrap them (e.g. S3UploadFailedError), so `except
        # _UploadTooLarge` at the call site is not reliable. This flag
        # is checked directly after the upload fails.
        self.exceeded = False

    def read(self, n: int = -1) -> bytes:
        chunk = self._source.read(n)
        self._read += len(chunk)
        if self._read > self._limit:
            self.exceeded = True
            raise _UploadTooLarge()
        return chunk


def _source_suffix(filename: str | None) -> str:
    if filename:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix in ALLOWED_VIDEO_SUFFIXES:
            return suffix
    return ".mp4"


def _safe_clip_stem(filename: str | None) -> str:
    """Sanitised, extensionless slug of an uploaded filename. Mirrors
    pipeline.orchestrator._safe_stem (kept local so the web process
    doesn't import the pipeline package). Idempotent under it."""
    base = PurePosixPath(filename or "").stem
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_-")[:50].strip("_-")
    return slug or "clip"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pipeline subprocess
# ---------------------------------------------------------------------------


def _run_pipeline_subprocess(source_path: str, work_dir: str) -> dict[str, Any]:
    """Top-level callable invoked by the ProcessPoolExecutor.

    Must be importable at module scope so it can be pickled across the pool
    boundary.
    """
    from pipeline.orchestrator import _safe_stem, make_zip, run_pipeline

    src = Path(source_path)
    out_dir = Path(work_dir) / "out"
    deliverables = run_pipeline(src, out_dir, interpolate=True, keep_intermediate=False)

    # Source file is named after the uploaded clip (see
    # _run_pipeline_for_job), so its stem drives the bundle folder name.
    stem = _safe_stem(src.stem)
    bundle = Path(work_dir) / "bundle.zip"
    make_zip(deliverables, bundle, root_dir=stem)

    duration: float | None = None
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(src),
            ],
            capture_output=True, text=True, check=True,
        )
        duration = float(probe.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        duration = None

    return {"bundle_path": str(bundle), "duration_seconds": duration}


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


async def _delete_quietly(storage: Storage, key: str, job_id: uuid.UUID) -> None:
    """Best-effort delete; never raises (used on cleanup paths)."""
    try:
        await storage.delete(key)
    except Exception:
        logger.exception("failed to delete %s for job %s", key, job_id)


async def _run_pipeline_for_job(
    job_id: uuid.UUID,
    source_key: str,
    storage: Storage,
    clip_stem: str,
) -> None:
    record = _jobs[job_id]
    record.status = JobStatus.running
    record.started_at = _utcnow()

    work_dir = Path(tempfile.mkdtemp(prefix=f"facecap_job_{job_id}_"))
    suffix = PurePosixPath(source_key).suffix.lower() or ".mp4"
    # Name the working source after the uploaded clip so the pipeline's
    # deliverables (and the bundle folder) are named after it too.
    source_path = work_dir / f"{clip_stem}{suffix}"
    bundle_key: str | None = None

    try:
        await storage.get_file(source_key, source_path)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            get_executor(),
            _run_pipeline_subprocess,
            str(source_path),
            str(work_dir),
        )

        bundle_key = f"bundles/{job_id}.zip"
        await storage.put_file(
            bundle_key,
            Path(result["bundle_path"]),
            content_type="application/zip",
        )

        record.bundle_key = bundle_key
        record.source_duration_seconds = result.get("duration_seconds")
        record.completed_at = _utcnow()
        record.status = JobStatus.succeeded

        try:
            await storage.delete(source_key)
        except Exception:
            logger.exception("failed to delete source for job %s", job_id)

        logger.info("job %s succeeded", job_id)
    except asyncio.CancelledError:
        logger.warning("job %s cancelled mid-flight", job_id)
        record.status = JobStatus.failed
        record.error_log = "Job cancelled (service shutdown)."
        record.completed_at = _utcnow()
        if bundle_key is not None:
            try:
                await storage.delete(bundle_key)
            except Exception:
                logger.exception("failed to delete orphan bundle for job %s", job_id)
        await _delete_quietly(storage, source_key, job_id)
        raise
    except Exception:
        tb = traceback.format_exc()
        logger.exception("job %s failed", job_id)
        record.error_log = tb[:_ERROR_LOG_MAX]
        record.completed_at = _utcnow()
        record.status = JobStatus.failed
        if bundle_key is not None:
            try:
                await storage.delete(bundle_key)
            except Exception:
                logger.exception("failed to delete partial bundle for job %s", job_id)
        await _delete_quietly(storage, source_key, job_id)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api")


@router.post("/jobs", status_code=status.HTTP_201_CREATED, response_model=JobCreated)
async def create_job(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[Storage, Depends(get_storage)],
    video: Annotated[UploadFile, File(description="Source video (multipart field 'video').")],
    content_length: Annotated[int | None, Header(alias="content-length")] = None,
) -> JobCreated:
    if (
        content_length is not None
        and content_length > settings.max_upload_bytes + _CONTENT_LENGTH_HEADROOM
    ):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds {settings.max_upload_bytes} bytes.",
        )

    content_type = video.content_type or ""
    if content_type and not content_type.startswith("video/"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Expected a video/* upload.",
        )

    job_id = uuid.uuid4()
    source_key = f"sources/{job_id}{_source_suffix(video.filename)}"

    bounded = _BoundedReader(video.file, settings.max_upload_bytes)
    try:
        await storage.put_stream(
            source_key,
            bounded,
            content_type=content_type or "application/octet-stream",
        )
    except Exception as exc:
        await storage.delete(source_key)
        # Either the raw exception, or — if boto3 swallowed/wrapped it —
        # the reader's own flag tells us the client exceeded the limit.
        if isinstance(exc, _UploadTooLarge) or bounded.exceeded:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload exceeds {settings.max_upload_bytes} bytes.",
            ) from exc
        raise

    clip_stem = _safe_clip_stem(video.filename)
    now = _utcnow()
    record = JobRecord(
        id=job_id, status=JobStatus.queued, created_at=now,
        clip_stem=clip_stem,
    )
    _jobs[job_id] = record

    asyncio.create_task(
        _run_pipeline_for_job(job_id, source_key, storage, clip_stem),
        name=f"job-{job_id}",
    )

    return JobCreated(id=job_id, status=record.status, created_at=now)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: uuid.UUID) -> JobDetail:
    record = _jobs.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return JobDetail(
        id=record.id,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        error_log=record.error_log,
        source_duration_seconds=record.source_duration_seconds,
    )


@router.get("/jobs/{job_id}/bundle", response_model=BundleDownload)
async def get_job_bundle(
    job_id: uuid.UUID,
    storage: Annotated[Storage, Depends(get_storage)],
) -> BundleDownload:
    record = _jobs.get(job_id)
    if record is None:
        # Job state is in-memory; a service restart drops it (PRD §5.3).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "This job is no longer available (the server was "
                "restarted). Please upload your video again."
            ),
        )
    if record.status != JobStatus.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Bundle not available; job status is {record.status}.",
        )
    if record.bundle_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job succeeded but no bundle is recorded.",
        )
    # The bundle object may have aged out of storage (R2 lifecycle /
    # local TTL) even though the in-memory record survives. Verify it
    # exists so the user gets a clean in-app error instead of being
    # redirected to a raw storage 404 page.
    if not await storage.exists(record.bundle_key):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "This bundle has expired and is no longer available. "
                "Please upload your video again."
            ),
        )
    url = storage.signed_download_url(
        record.bundle_key,
        expires_in=DOWNLOAD_URL_TTL_SECONDS,
        download_name=f"{record.clip_stem}.zip",
    )
    return BundleDownload(url=url, expires_in=DOWNLOAD_URL_TTL_SECONDS)
