"""Shared-key authentication.

Every protected route depends on `require_api_key`. The check is a constant-time
comparison against `settings.api_key` to avoid timing leaks.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


API_KEY_HEADER = "X-API-Key"


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
