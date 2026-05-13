"""Health endpoint.

Public (no auth) — used by Railway for liveness checks.
"""

from __future__ import annotations

from fastapi import APIRouter


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
