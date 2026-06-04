"""Unit tests for the sliding-window rate limiter and client-IP extraction."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.ratelimit import RateLimiter, client_identifier


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_allows_up_to_limit_then_blocks() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(3, 60, clock=clock)

    results = [limiter.try_acquire("ip") for _ in range(3)]
    assert all(r.allowed for r in results)
    assert [r.remaining for r in results] == [2, 1, 0]

    blocked = limiter.try_acquire("ip")
    assert not blocked.allowed
    # Oldest event was at t0; window is 60s, so retry in ~60s.
    assert blocked.retry_after == pytest.approx(60.0)


def test_window_slides_and_frees_capacity() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(2, 60, clock=clock)

    assert limiter.try_acquire("ip").allowed
    clock.advance(30)
    assert limiter.try_acquire("ip").allowed
    assert not limiter.try_acquire("ip").allowed  # full

    # 31s later the first event (t=1000) has aged out; capacity returns.
    clock.advance(31)
    assert limiter.try_acquire("ip").allowed


def test_keys_are_independent() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    assert limiter.try_acquire("a").allowed
    assert limiter.try_acquire("b").allowed  # different key, own budget
    assert not limiter.try_acquire("a").allowed


def test_lru_eviction_bounds_key_count() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(1, 3600, max_keys=2, clock=clock)
    limiter.try_acquire("a")
    limiter.try_acquire("b")
    limiter.try_acquire("c")  # evicts "a" (least recently used)
    # "a" was forgotten, so it starts fresh and is allowed again.
    assert limiter.try_acquire("a").allowed
    # The map never exceeds max_keys.
    assert len(limiter._hits) <= 2


def test_rejects_invalid_config() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0, 60)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)


# --- client_identifier -----------------------------------------------------


@dataclass
class _Client:
    host: str


class _Req:
    """Minimal stand-in for starlette Request (headers + client)."""

    def __init__(self, headers: dict[str, str], client_host: str | None) -> None:
        # starlette headers are case-insensitive; tests use lowercase keys.
        self.headers = headers
        self.client = _Client(client_host) if client_host else None


def test_client_ip_prefers_rightmost_forwarded_entry() -> None:
    # A client trying to spoof puts a fake IP first; the proxy appends the
    # real one. Rightmost (single hop) is the trustworthy value.
    req = _Req({"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, "10.0.0.1")
    assert client_identifier(req, trust_proxy=True) == "203.0.113.7"


def test_client_ip_honours_extra_hops() -> None:
    req = _Req({"x-forwarded-for": "203.0.113.7, proxy1, proxy2"}, "10.0.0.1")
    assert client_identifier(req, trust_proxy=True, proxy_hops=2) == "proxy1"


def test_client_ip_falls_back_to_peer_when_untrusted() -> None:
    req = _Req({"x-forwarded-for": "9.9.9.9"}, "10.0.0.1")
    assert client_identifier(req, trust_proxy=False) == "10.0.0.1"


def test_client_ip_handles_missing_client() -> None:
    req = _Req({}, None)
    assert client_identifier(req, trust_proxy=True) == "unknown"
