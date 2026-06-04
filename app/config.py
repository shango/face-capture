"""Application settings loaded from environment variables.

A single `Settings` instance is constructed at import time via `get_settings()`
and cached. All env reads go through this module — no `os.environ` access
elsewhere in the app.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Storage backend selector ---
    storage_backend: Literal["local", "r2"] = Field(
        default="local",
        description="Which Storage impl to use. 'local' for dev, 'r2' in production.",
    )

    # --- LocalStorage (dev fallback) ---
    local_storage_dir: Path = Field(
        default=REPO_ROOT / "_storage",
        description="Filesystem root for LocalStorage backend.",
    )

    # --- Cloudflare R2 (S3-compatible) ---
    r2_endpoint_url: str | None = Field(
        default=None,
        description="https://<account>.r2.cloudflarestorage.com",
    )
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None
    r2_region: str = "auto"

    # --- Upload ---
    max_upload_bytes: int = Field(
        default=500 * 1024 * 1024,
        description="Hard limit on uploaded video size.",
    )

    # --- Abuse / rate limiting ---
    # Per-client-IP, enforced in-process (single replica; see app/ratelimit.py).
    rate_limit_enabled: bool = Field(
        default=True,
        description="Master switch for per-IP rate limiting.",
    )
    upload_rate_max: int = Field(
        default=5,
        description="Max job uploads per IP within upload_rate_window_seconds.",
    )
    upload_rate_window_seconds: int = Field(
        default=600,
        description="Sliding window (s) for the per-IP upload limit.",
    )
    read_rate_max: int = Field(
        default=120,
        description="Max status/bundle reads per IP within read_rate_window_seconds.",
    )
    read_rate_window_seconds: int = Field(
        default=60,
        description="Sliding window (s) for the per-IP read limit.",
    )
    max_active_jobs: int = Field(
        default=4,
        description=(
            "Global cap on queued+running jobs. New uploads are refused with "
            "503 once reached — the backstop against a distributed flood, since "
            "the pipeline (single worker) is the cost center."
        ),
    )
    trust_proxy: bool = Field(
        default=True,
        description=(
            "Derive the client IP from X-Forwarded-For. True in production "
            "(behind Railway's proxy); set False if the app is exposed directly."
        ),
    )
    trusted_proxy_hops: int = Field(
        default=1,
        description=(
            "Number of trusted proxy hops; the client IP is taken this many "
            "entries from the right of X-Forwarded-For. Raise if Railway adds "
            "additional internal hops in front of the app."
        ),
    )

    # --- Static SPA ---
    web_dist_dir: Path = Field(
        default=REPO_ROOT / "web" / "dist",
        description="Directory containing the built Vite SPA.",
    )

    # --- Dev CORS (leave empty in prod; same-origin in prod) ---
    cors_origins: list[str] = Field(
        default_factory=list,
        description="Origins allowed in dev (e.g., http://localhost:5173).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
