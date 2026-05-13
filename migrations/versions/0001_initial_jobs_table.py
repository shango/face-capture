"""initial jobs table

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JOB_STATUS_VALUES = ("queued", "running", "succeeded", "failed")


def upgrade() -> None:
    # gen_random_uuid() lives in pgcrypto on PG <13 and core on PG 13+; the
    # extension call is idempotent and works on either.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    job_status = postgresql.ENUM(*JOB_STATUS_VALUES, name="job_status")
    job_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(*JOB_STATUS_VALUES, name="job_status", create_type=False),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("source_video_key", sa.Text(), nullable=False),
        sa.Column("bundle_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column(
            "pipeline_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source_duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_jobs_status_created_at", "jobs", ["status", "created_at"],
    )
    op.create_index("ix_jobs_expires_at", "jobs", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_expires_at", table_name="jobs")
    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.drop_table("jobs")
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
