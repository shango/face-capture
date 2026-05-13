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

    # --- Auth ---
    api_key: str = Field(..., description="Shared secret required in X-API-Key header.")

    # --- Database ---
    # Example: postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str = Field(..., description="SQLAlchemy async URL (asyncpg driver).")

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

    # --- Job lifecycle ---
    bundle_ttl_days: int = Field(
        default=7,
        ge=1,
        description="How long completed bundles remain downloadable.",
    )
    max_upload_bytes: int = Field(
        default=500 * 1024 * 1024,
        description="Hard limit on uploaded video size (PRD §6.1).",
    )

    # --- Static SPA ---
    web_dist_dir: Path = Field(
        default=REPO_ROOT / "web" / "dist",
        description="Directory containing the built Vite SPA (index.html + assets).",
    )

    # --- Misc ---
    cors_origins: list[str] = Field(
        default_factory=list,
        description="Origins allowed in dev (e.g., http://localhost:5173). Empty in prod.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
