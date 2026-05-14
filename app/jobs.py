"""Job HTTP endpoints.

Routes (all under `/api` and gated by `X-API-Key`):

  POST   /api/jobs                — multipart upload, returns {id, status, created_at}
  GET    /api/jobs/{id}           — current status snapshot
  GET    /api/jobs/{id}/bundle    — signed URL for the completed bundle

The POST handler streams the upload to storage, then inserts the `jobs` row.
On any failure after the upload starts, the partial object is deleted so we
do not leak orphan files. The cleanup task (#7) is a safety net for crashes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, BinaryIO

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_api_key
from .config import Settings, get_settings
from .db import session_dependency
from .models import Job, JobStatus
from .storage import Storage, get_storage


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class JobCreated(BaseModel):
    id: uuid.UUID
    status: JobStatus
    created_at: datetime


class JobDetail(BaseModel):
    """Full status view of a job — what the SPA polls."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_log: str | None
    source_duration_seconds: float | None
    expires_at: datetime | None


class BundleDownload(BaseModel):
    url: str
    expires_in: int


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


ALLOWED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
DOWNLOAD_URL_TTL_SECONDS = 3600
# Content-Length covers the whole multipart envelope, not just the file. Allow
# this much headroom for framing before rejecting a request outright; the
# bounded reader still enforces the precise file-size limit.
_CONTENT_LENGTH_HEADROOM = 1 * 1024 * 1024


class _UploadTooLarge(Exception):
    """Raised by `_BoundedReader` when the upload exceeds the size limit."""


class _BoundedReader:
    """Wraps a sync file-like object and aborts after `limit` bytes are read.

    Used to enforce `settings.max_upload_bytes` while streaming to storage.
    Starlette buffers the multipart body before we run, so this is defense
    in depth — a proxy-level limit is the real protection at the edge.
    """

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._limit = limit
        self._read = 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._source.read(n)
        self._read += len(chunk)
        if self._read > self._limit:
            raise _UploadTooLarge()
        return chunk


def _source_suffix(filename: str | None) -> str:
    """Pick a safe filename suffix for the stored source video."""
    if filename:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix in ALLOWED_VIDEO_SUFFIXES:
            return suffix
    return ".mp4"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


@router.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    response_model=JobCreated,
)
async def create_job(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[Storage, Depends(get_storage)],
    session: Annotated[AsyncSession, Depends(session_dependency)],
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
    except _UploadTooLarge:
        await storage.delete(source_key)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds {settings.max_upload_bytes} bytes.",
        )
    except Exception:
        await storage.delete(source_key)
        raise

    job = Job(
        id=job_id,
        status=JobStatus.queued,
        source_video_key=source_key,
        pipeline_config={},
    )
    session.add(job)
    try:
        await session.flush()
        await session.refresh(job)
    except Exception:
        # Row insert failed — drop the uploaded source to avoid an orphan.
        await storage.delete(source_key)
        raise

    return JobCreated(id=job.id, status=job.status, created_at=job.created_at)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(session_dependency)],
) -> JobDetail:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return JobDetail.model_validate(job)


@router.get("/jobs/{job_id}/bundle", response_model=BundleDownload)
async def get_job_bundle(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(session_dependency)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> BundleDownload:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    if job.status != JobStatus.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Bundle not available; job status is {job.status.value}.",
        )

    if job.bundle_key is None:
        # Should not happen for a succeeded job; treat as 500 since the worker
        # contract is to set bundle_key before flipping status.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job succeeded but no bundle is recorded.",
        )

    if job.expires_at is not None and job.expires_at < _utcnow():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Bundle has expired.",
        )

    url = storage.signed_download_url(
        job.bundle_key,
        expires_in=DOWNLOAD_URL_TTL_SECONDS,
    )
    return BundleDownload(url=url, expires_in=DOWNLOAD_URL_TTL_SECONDS)
