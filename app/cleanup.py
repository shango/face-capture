"""Periodic cleanup of expired artifacts and stuck jobs.

A FastAPI-lifespan asyncio task fires every ``cleanup_interval_seconds``:

  1. **Stuck running jobs.** A row whose ``started_at`` is older than
     ``cleanup_stuck_running_threshold_seconds`` is presumed crashed and is
     marked ``failed``. The worker reverts to ``queued`` on graceful
     cancellation, so this only triggers when the process was killed before
     cleanup could run. The threshold must comfortably exceed the longest
     expected pipeline run; the update is conditional on ``status=running``
     so a worker that finishes during the same sweep wins the race cleanly.

  2. **Expired jobs.** Rows whose ``expires_at`` has passed are removed:
     the bundle is deleted from storage; failed-job sources are also
     deleted (succeeded jobs already had their source deleted by the
     worker); then the row itself is dropped. Artifact deletes happen
     outside any DB transaction so a slow storage call cannot hold row
     locks. Deletes are idempotent, so a second instance racing the same
     sweep is harmless.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from .config import Settings
from .db import get_session_factory
from .models import Job, JobStatus
from .storage import Storage

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_STUCK_ERROR_MSG = (
    "Job marked failed by cleanup: service crashed before the worker could "
    "record completion."
)


class Cleanup:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self._settings = settings
        self._storage = storage
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Cleanup already started.")
        self._task = asyncio.create_task(self._run(), name="cleanup")
        logger.info(
            "cleanup started (interval=%.0fs, stuck_threshold=%.0fs)",
            self._settings.cleanup_interval_seconds,
            self._settings.cleanup_stuck_running_threshold_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("cleanup stopped")

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        interval = self._settings.cleanup_interval_seconds
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("cleanup tick failed; will retry next interval")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        """Run a single cleanup sweep. Exposed for tests."""
        marked = await self._fail_stuck_running()
        if marked:
            logger.info("marked %d stuck running job(s) as failed", marked)
        purged = await self._purge_expired()
        if purged:
            logger.info("purged %d expired job(s)", purged)

    # ----------------------------------------------------- stuck running

    async def _fail_stuck_running(self) -> int:
        now = _utcnow()
        threshold = now - timedelta(
            seconds=self._settings.cleanup_stuck_running_threshold_seconds
        )
        expires_at = now + timedelta(days=self._settings.bundle_ttl_days)
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                # Atomic conditional update: Postgres locks each row during the
                # write, so a worker mid-UPDATE on the same row serializes with
                # us. If the worker wins, its status flip removes the row from
                # this WHERE clause and the row is skipped.
                stmt = (
                    update(Job)
                    .where(Job.status == JobStatus.running)
                    .where(Job.started_at.is_not(None))
                    .where(Job.started_at < threshold)
                    .values(
                        status=JobStatus.failed,
                        completed_at=now,
                        expires_at=expires_at,
                        error_log=_STUCK_ERROR_MSG,
                    )
                    .returning(Job.id)
                )
                result = await session.execute(stmt)
                return len(result.all())

    # -------------------------------------------------------- purge expired

    async def _purge_expired(self) -> int:
        now = _utcnow()
        session_factory = get_session_factory()

        # Phase 1: read expired rows. No row locks held while we delete
        # artifacts in phase 2.
        async with session_factory() as session:
            stmt = (
                select(
                    Job.id,
                    Job.source_video_key,
                    Job.bundle_key,
                    Job.status,
                )
                .where(Job.expires_at.is_not(None))
                .where(Job.expires_at < now)
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            return 0

        ids: list[uuid.UUID] = []
        for job_id, source_key, bundle_key, job_status in rows:
            if bundle_key is not None:
                try:
                    await self._storage.delete(bundle_key)
                except Exception:
                    logger.exception(
                        "cleanup: failed to delete bundle %s (job %s)",
                        bundle_key,
                        job_id,
                    )
            # Succeeded jobs already had their source deleted by the worker;
            # failed (or stuck-failed) jobs kept theirs for diagnosis.
            if job_status == JobStatus.failed:
                try:
                    await self._storage.delete(source_key)
                except Exception:
                    logger.exception(
                        "cleanup: failed to delete source %s (job %s)",
                        source_key,
                        job_id,
                    )
            ids.append(job_id)

        # Phase 3: drop the rows. Idempotent if another instance got here first.
        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(Job).where(Job.id.in_(ids)))

        return len(ids)
