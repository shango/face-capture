"""FastAPI application entry point.

Mounts:
  - /health           liveness probe (public)
  - /api/...          JSON API (gated by X-API-Key; wired in later tasks)
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # DB engine, worker task, and cleanup task get wired here in later tasks.
    yield


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
            allow_headers=["X-API-Key", "Content-Type"],
        )

    app.include_router(health_router)

    # SPA static serving: only mounted when the Vite build exists. In dev,
    # the frontend runs on Vite's own server and proxies /api + /health here.
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

        # Catch-all for client-side routing. Must be registered AFTER /api and
        # /health routers so they take precedence. (Currently there is no
        # client-side routing — this is forward-compatible.)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_catch_all(full_path: str) -> FileResponse:
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
