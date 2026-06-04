"""Integration tests for rate limiting, load shedding, and security headers.

These build a fresh app per case (so env-driven settings take effect), stub
out object storage, and no-op the pipeline runner so an upload is accepted
without kicking off real CPU work.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastapi.testclient import TestClient

import app.config
import app.jobs
import app.main
import app.storage


class FakeStorage:
    """In-memory Storage stand-in covering the create-job code path."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, source: BinaryIO, *, content_type: str) -> None:
        data = b""
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            data += chunk
        self.objects[key] = data

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        self.objects[key] = path.read_bytes()

    async def get_file(self, key: str, dest: Path) -> None:  # pragma: no cover
        dest.write_bytes(self.objects[key])

    def signed_download_url(self, key: str, *, expires_in: int = 3600, download_name: str | None = None) -> str:
        return f"https://example.test/{key}"

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.fixture
def make_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Factory: build an app with the given settings env overrides."""

    def _make(**env: Any) -> Any:
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
        monkeypatch.setenv("WEB_DIST_DIR", str(tmp_path / "nonexistent_dist"))
        for key, value in env.items():
            monkeypatch.setenv(key.upper(), str(value))

        app.config.get_settings.cache_clear()
        app.storage.get_storage.cache_clear()
        app.jobs._jobs.clear()

        async def _noop_runner(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(app.jobs, "_run_pipeline_for_job", _noop_runner)

        application = app.main.create_app()
        application.dependency_overrides[app.storage.get_storage] = lambda: FakeStorage()
        return application

    yield _make

    app.config.get_settings.cache_clear()
    app.storage.get_storage.cache_clear()
    app.jobs._jobs.clear()


_VIDEO = {"video": ("clip.mp4", b"\x00\x00fakevideo", "video/mp4")}


def _upload(client: TestClient, headers: dict[str, str] | None = None):
    return client.post("/api/jobs", files=_VIDEO, headers=headers or {})


def test_security_headers_present(make_app: Any) -> None:
    client = TestClient(make_app())
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_upload_rate_limit_returns_429(make_app: Any) -> None:
    client = TestClient(
        make_app(upload_rate_max=2, upload_rate_window_seconds=600, max_active_jobs=100)
    )
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 201
    third = _upload(client)
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) >= 1


def test_upload_rate_limit_is_per_ip(make_app: Any) -> None:
    client = TestClient(make_app(upload_rate_max=1, max_active_jobs=100))
    assert _upload(client, {"X-Forwarded-For": "1.1.1.1"}).status_code == 201
    assert _upload(client, {"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    # A different client IP has its own budget.
    assert _upload(client, {"X-Forwarded-For": "2.2.2.2"}).status_code == 201


def test_active_job_cap_returns_503(make_app: Any) -> None:
    # Runner is a no-op, so accepted jobs stay 'queued' and count as active.
    client = TestClient(make_app(max_active_jobs=1, upload_rate_max=100))
    assert _upload(client).status_code == 201
    busy = _upload(client)
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "60"


def test_read_endpoint_is_rate_limited(make_app: Any) -> None:
    client = TestClient(make_app(read_rate_max=2, read_rate_window_seconds=60))
    missing = f"/api/jobs/{uuid.uuid4()}"
    assert client.get(missing).status_code == 404  # passes limiter, then 404
    assert client.get(missing).status_code == 404
    assert client.get(missing).status_code == 429


def test_rate_limiting_can_be_disabled(make_app: Any) -> None:
    client = TestClient(
        make_app(rate_limit_enabled="false", upload_rate_max=1, max_active_jobs=100)
    )
    for _ in range(3):
        assert _upload(client).status_code == 201
