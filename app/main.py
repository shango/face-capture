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
from .ratelimit import RateLimiter
from .storage import LocalStorage, get_storage, make_local_storage_router


# Hardening headers added to every response. Kept conservative: this API
# serves JSON and a self-contained SPA from the same origin, so framing and
# MIME-sniffing are never legitimate.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


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

    # Per-IP limiters live on app.state so they share one instance across
    # requests (and are trivially swappable in tests). Single replica → a
    # process-local limiter is sufficient; see app/ratelimit.py.
    app.state.upload_limiter = RateLimiter(
        settings.upload_rate_max, settings.upload_rate_window_seconds
    )
    app.state.read_limiter = RateLimiter(
        settings.read_rate_max, settings.read_rate_window_seconds
    )

    @app.middleware("http")
    async def add_security_headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

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

        # index.html must never be cached: it points at content-hashed
        # asset filenames, so a stale shell keeps loading old JS/CSS
        # after a deploy. The hashed assets themselves are immutable.
        _INDEX_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

        @app.get("/", include_in_schema=False)
        async def spa_index() -> FileResponse:
            return FileResponse(dist / "index.html", headers=_INDEX_HEADERS)

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
                return FileResponse(index, headers=_INDEX_HEADERS)
            return FileResponse(candidate)

    return app


app = create_app()
