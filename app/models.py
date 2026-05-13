"""SQLAlchemy ORM models.

Currently a single `Job` table — see PRD §6.4. The `studio_id` column from the
PRD is deferred until accounts are added (v2.1+).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Enum, Index, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True, validate_strings=True),
        nullable=False,
        default=JobStatus.queued,
        server_default=JobStatus.queued.value,
    )
    source_video_key: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    source_duration_seconds: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

    __table_args__ = (
        # Worker dequeue path: pick the oldest queued job.
        Index("ix_jobs_status_created_at", "status", "created_at"),
        # Cleanup task: find rows past expires_at.
        Index("ix_jobs_expires_at", "expires_at"),
    )
