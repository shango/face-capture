"""FastAPI application entry point.

Mounts:
  - /health           liveness probe (public)
  - /api/...          JSON API (public — no auth)
  - /assets, /*       built Vite SPA from `settings.web_dist_dir` (production)

In dev the SPA is served by Vite on its own port; the FastAPI app only serves
/health and /api. The static mount is skipped automatically when web/dist is
absent.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .health import router as health_router
from .jobs import router as jobs_router, shutdown_executor
from .storage import LocalStorage, get_storage, make_local_storage_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        shutdown_executor()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="face-capture",
        version="2.0.0",
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    app.include_router(health_router)
    app.include_router(jobs_router)

    # When the LocalStorage backend is active, mount its signed-URL serving
    # route so the SPA can download bundles via the same origin. With R2 the
    # signed URL points to Cloudflare directly, so no mount is needed.
    if settings.storage_backend == "local":
        storage = get_storage()
        assert isinstance(storage, LocalStorage)
        app.include_router(make_local_storage_router(storage))

    # SPA static serving: only mounted when the Vite build exists.
    dist = settings.web_dist_dir
    if dist.is_dir() and (dist / "index.html").is_file():
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="assets",
            )

        @app.get("/", include_in_schema=False)
        async def spa_index() -> FileResponse:
            return FileResponse(dist / "index.html")

        dist_root = dist.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_catch_all(full_path: str) -> FileResponse:
            index = dist_root / "index.html"
            # Contain the resolved path under dist_root: a request like
            # `/../../etc/passwd` (or %2e%2e-encoded) must not escape the
            # build directory. Fall back to the SPA shell otherwise.
            candidate = (dist_root / full_path).resolve()
            if (
                candidate == dist_root
                or dist_root not in candidate.parents
                or not candidate.is_file()
            ):
                return FileResponse(index)
            return FileResponse(candidate)

    return app


app = create_app()
