"""In-service job worker.

A FastAPI-lifespan asyncio task polls the `jobs` table for queued rows,
claims them with ``SELECT ... FOR UPDATE SKIP LOCKED``, and runs the capture
pipeline in a ``ProcessPoolExecutor`` subprocess. Storage IO (download
source, upload bundle) happens in the async parent process; only the heavy
pipeline itself runs out-of-process so we keep the event loop responsive.

On success: uploads ``bundles/<job-id>.zip``, records ``bundle_key`` and
``expires_at``, sets status to ``succeeded``, and deletes the source. On
failure: records a truncated traceback in ``error_log``, sets status to
``failed``, and leaves the source in place for diagnosis. The cleanup task
in #7 is the safety net for crashes between pipeline exit and DB update.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select, update

from .config import Settings
from .db import get_session_factory
from .models import Job, JobStatus
from .storage import Storage

logger = logging.getLogger(__name__)


_ERROR_LOG_MAX = 8000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _source_local_suffix(source_key: str) -> str:
    """Preserve the source video's suffix locally so ffmpeg can sniff format."""
    suffix = PurePosixPath(source_key).suffix.lower()
    return suffix or ".mp4"


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------


def _run_pipeline_subprocess(source_path: str, work_dir: str) -> dict[str, Any]:
    """Top-level callable invoked by the ``ProcessPoolExecutor`` worker.

    Returns the bundle zip path and the detected source duration. Must be
    importable at module scope so it can be pickled across the pool boundary.
    """
    from pipeline.orchestrator import make_zip, run_pipeline

    src = Path(source_path)
    out_dir = Path(work_dir) / "out"
    deliverables = run_pipeline(src, out_dir, interpolate=True, keep_intermediate=False)

    bundle = Path(work_dir) / "bundle.zip"
    make_zip(deliverables, bundle)

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
# Worker
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self._settings = settings
        self._storage = storage
        self._stop = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()
        self._executor = ProcessPoolExecutor(max_workers=settings.worker_concurrency)
        self._poller: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._poller is not None:
            raise RuntimeError("Worker already started.")
        self._poller = asyncio.create_task(self._run(), name="job-worker-poller")
        logger.info(
            "worker started (concurrency=%d, poll_interval=%.2fs)",
            self._settings.worker_concurrency,
            self._settings.worker_poll_interval_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._poller is not None:
            try:
                await self._poller
            except asyncio.CancelledError:
                pass
            self._poller = None

        if self._inflight:
            timeout = self._settings.worker_shutdown_timeout_seconds
            logger.info("waiting up to %.1fs for %d in-flight job(s)", timeout, len(self._inflight))
            done, pending = await asyncio.wait(self._inflight, timeout=timeout)
            for task in pending:
                logger.warning("job task %s did not finish in time; cancelling", task.get_name())
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("worker stopped")

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        interval = self._settings.worker_poll_interval_seconds
        concurrency = self._settings.worker_concurrency
        while not self._stop.is_set():
            if len(self._inflight) >= concurrency:
                await self._sleep_or_stop(interval)
                continue

            try:
                claimed = await self._claim_one()
            except Exception:
                logger.exception("claim attempt failed; backing off")
                await self._sleep_or_stop(interval)
                continue

            if claimed is None:
                await self._sleep_or_stop(interval)
                continue

            job_id, source_key = claimed
            logger.info("claimed job %s", job_id)
            task = asyncio.create_task(self._run_job(job_id, source_key), name=f"job-{job_id}")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    # --------------------------------------------------------------- claim

    async def _claim_one(self) -> tuple[uuid.UUID, str] | None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                stmt = (
                    select(Job.id, Job.source_video_key)
                    .where(Job.status == JobStatus.queued)
                    .order_by(Job.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                row = (await session.execute(stmt)).first()
                if row is None:
                    return None
                job_id: uuid.UUID = row[0]
                source_key: str = row[1]
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(status=JobStatus.running, started_at=_utcnow())
                )
                return job_id, source_key

    # ----------------------------------------------------------- run a job

    async def _run_job(self, job_id: uuid.UUID, source_key: str) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix=f"facecap_job_{job_id}_"))
        source_path = work_dir / f"source{_source_local_suffix(source_key)}"
        bundle_key: str | None = None
        try:
            await self._storage.get_file(source_key, source_path)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                _run_pipeline_subprocess,
                str(source_path),
                str(work_dir),
            )
            bundle_key = f"bundles/{job_id}.zip"
            await self._storage.put_file(
                bundle_key,
                Path(result["bundle_path"]),
                content_type="application/zip",
            )
            await self._mark_succeeded(
                job_id,
                bundle_key=bundle_key,
                duration_seconds=result.get("duration_seconds"),
            )
            try:
                await self._storage.delete(source_key)
            except Exception:
                logger.exception("failed to delete source for job %s", job_id)
            logger.info("job %s succeeded", job_id)
        except asyncio.CancelledError:
            logger.warning("job %s cancelled mid-flight; reverting to queued", job_id)
            try:
                await self._revert_to_queued(job_id)
            except Exception:
                logger.exception("failed to revert job %s to queued", job_id)
            if bundle_key is not None:
                try:
                    await self._storage.delete(bundle_key)
                except Exception:
                    logger.exception("failed to delete orphan bundle for job %s", job_id)
            raise
        except Exception:
            tb = traceback.format_exc()
            logger.exception("job %s failed", job_id)
            try:
                await self._mark_failed(job_id, tb)
            except Exception:
                logger.exception("failed to record failure for job %s", job_id)
            if bundle_key is not None:
                try:
                    await self._storage.delete(bundle_key)
                except Exception:
                    logger.exception("failed to delete partial bundle for job %s", job_id)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # -------------------------------------------------------- DB mutations

    async def _mark_succeeded(
        self,
        job_id: uuid.UUID,
        *,
        bundle_key: str,
        duration_seconds: float | None,
    ) -> None:
        now = _utcnow()
        expires_at = now + timedelta(days=self._settings.bundle_ttl_days)
        values: dict[str, Any] = {
            "status": JobStatus.succeeded,
            "completed_at": now,
            "bundle_key": bundle_key,
            "expires_at": expires_at,
        }
        if duration_seconds is not None:
            values["source_duration_seconds"] = Decimal(str(duration_seconds))
        await self._update_job(job_id, values)

    async def _mark_failed(self, job_id: uuid.UUID, traceback_str: str) -> None:
        now = _utcnow()
        expires_at = now + timedelta(days=self._settings.bundle_ttl_days)
        await self._update_job(
            job_id,
            {
                "status": JobStatus.failed,
                "completed_at": now,
                "expires_at": expires_at,
                "error_log": traceback_str[:_ERROR_LOG_MAX],
            },
        )

    async def _revert_to_queued(self, job_id: uuid.UUID) -> None:
        await self._update_job(
            job_id,
            {"status": JobStatus.queued, "started_at": None},
        )

    async def _update_job(self, job_id: uuid.UUID, values: dict[str, Any]) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Job).where(Job.id == job_id).values(**values)
                )
