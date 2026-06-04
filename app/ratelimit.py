"""In-process, per-client rate limiting.

The service is single-replica with in-memory job state (see jobs.py), so a
process-local limiter is the correct fit — no external store required. A
multi-replica deployment would instead need a shared backend (e.g. Redis), at
which point this module should be swapped for one.

`RateLimiter` is a sliding-window log: it remembers the timestamps of recent
events per key and allows a new one only while fewer than `max_events` fall
inside the trailing `window_seconds`. Everything runs on the single asyncio
thread and `try_acquire` contains no `await`, so its read-modify-write is
atomic without locking.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Deque

from fastapi import Request


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    # Seconds until the caller may retry. 0 when allowed.
    retry_after: float


class RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string.

    Memory is bounded two ways: each key's deque only retains timestamps
    inside the window, and the key map is an LRU capped at `max_keys`, so a
    flood from many distinct clients cannot grow it without limit (the
    least-recently-seen keys are dropped first).
    """

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_events
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._clock = clock
        # Ordered so the front is the least-recently-touched key (LRU victim).
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()

    def try_acquire(self, key: str) -> RateLimitResult:
        """Record an event for `key` if under the limit; report the verdict."""
        now = self._clock()
        cutoff = now - self._window

        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        else:
            self._hits.move_to_end(key)

        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self._max:
            # The oldest event leaves the window self._window after it landed.
            retry_after = hits[0] + self._window - now
            return RateLimitResult(False, 0, max(retry_after, 0.0))

        hits.append(now)
        self._evict_if_needed()
        return RateLimitResult(True, self._max - len(hits), 0.0)

    def _evict_if_needed(self) -> None:
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)


def client_identifier(
    request: Request,
    *,
    trust_proxy: bool,
    proxy_hops: int = 1,
) -> str:
    """Best-effort stable identity for the requesting client.

    Behind a reverse proxy (Railway) the TCP peer is the proxy, so the real
    client must come from `X-Forwarded-For`. A proxy *appends* the connecting
    IP, so the genuine client as seen by the trusted proxy is the
    `proxy_hops`-th entry from the right — spoof-resistant, because any
    client-supplied XFF values sit to the *left* of what the proxy appended.
    With the default single hop this is the rightmost entry.

    `trust_proxy` must be False when the app is directly internet-facing, so a
    client cannot forge its identity by sending its own XFF header.
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                idx = max(0, len(parts) - max(proxy_hops, 1))
                return parts[idx]
    client = request.client
    return client.host if client else "unknown"
