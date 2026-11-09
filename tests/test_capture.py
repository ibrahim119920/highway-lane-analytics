"""Tests for downloading live windows recorded by the pipeline host."""

from __future__ import annotations

from pathlib import Path

import pytest

from lane_analytics.capture import CaptureDownload, CaptureError, fetch_capture

BASE = "https://lane-api.example"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, chunks=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._chunks = chunks or []
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield from self._chunks


class FakeSession:
    def __init__(self, post_response, get_response):
        self.post_response = post_response
        self.get_response = get_response
        self.calls: list[tuple] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        return self.post_response

    def get(self, url, stream=False, timeout=None):
        self.calls.append(("GET", url, stream, timeout))
        return self.get_response


def capture_payload(**overrides):
    payload = {
        "capture_id": "cap_20261109_100000_abcdef",
        "status": "ready",
        "duration_s": 30.0,
        "size_bytes": 6,
        "started_at": "2026-11-09T10:00:00",
        "backlog_s": 17.2,
        "download_url": f"{BASE}/capture/token.signature",
    }
    payload.update(overrides)
    return payload


def test_fetch_capture_downloads_and_reports_metadata(tmp_path: Path):
    session = FakeSession(
        FakeResponse(201, capture_payload()),
        FakeResponse(200, chunks=[b"abc", b"def"]),
    )
    result = fetch_capture(
        BASE,
        "secret",
        seconds=30,
        url="https://pub2.hk-opt.com/live.m3u8",
        destination=tmp_path,
        session=session,
    )

    assert result.capture_id == "cap_20261109_100000_abcdef"
    assert result.path == tmp_path / "cap_20261109_100000_abcdef.mp4"
    assert result.path.read_bytes() == b"abcdef"
    assert result.started_at == "2026-11-09T10:00:00"
    assert result.backlog_s == 17.2
    method, url, payload, headers = session.calls[0]
    assert (method, url) == ("POST", f"{BASE}/capture")
    assert payload == {
        "seconds": 30.0,
        "url": "https://pub2.hk-opt.com/live.m3u8",
    }
    assert headers["Authorization"] == "Bearer secret"


def test_fetch_capture_accepts_a_url_that_already_ends_with_capture(tmp_path: Path):
    session = FakeSession(
        FakeResponse(201, capture_payload()),
        FakeResponse(200, chunks=[b"abcdef"]),
    )
    fetch_capture(f"{BASE}/capture", "secret", destination=tmp_path, session=session)
    assert session.calls[0][1] == f"{BASE}/capture"


def test_fetch_capture_rejects_a_size_mismatch(tmp_path: Path):
    session = FakeSession(
        FakeResponse(201, capture_payload(size_bytes=99)),
        FakeResponse(200, chunks=[b"abc"]),
    )
    with pytest.raises(CaptureError, match="size mismatch"):
        fetch_capture(BASE, "secret", destination=tmp_path, session=session, sleep=lambda _: None)
    assert list(tmp_path.iterdir()) == []


def test_fetch_capture_reports_a_busy_host(tmp_path: Path):
    session = FakeSession(
        FakeResponse(409, {"detail": "another capture is already running"}),
        FakeResponse(200),
    )
    with pytest.raises(CaptureError, match="already recording"):
        fetch_capture(BASE, "secret", destination=tmp_path, session=session)


def test_fetch_capture_reports_bad_credentials(tmp_path: Path):
    session = FakeSession(FakeResponse(401), FakeResponse(200))
    with pytest.raises(CaptureError, match="rejected"):
        fetch_capture(BASE, "bad", destination=tmp_path, session=session)


def test_fetch_capture_needs_a_token(tmp_path: Path):
    with pytest.raises(CaptureError, match="token"):
        fetch_capture(BASE, "", destination=tmp_path)


def test_activate_server_capture_uses_host_start_time(tmp_path: Path, monkeypatch):
    import lane_analytics.capture as capture_module
    import lane_analytics.colab as colab

    video = tmp_path / "cap_20261109_100000_abcdef.mp4"
    video.write_bytes(b"fake")

    def fake_fetch(base, token, **kwargs):
        assert token == "pipeline-token"
        return CaptureDownload(
            capture_id="cap_20261109_100000_abcdef",
            path=video,
            started_at="2026-11-09T10:00:00",
            duration_s=30.0,
            size_bytes=4,
            backlog_s=17.2,
        )

    seen: dict = {}

    class FakeBatch:
        total_vehicles = 3
        total_events = 2
        hogging_events = 1
        evidence_events = 0
        evidence_snapshots = 0
        evidence_clips = 0
        evidence_dir = ""
        camera_id = "tj_priok_km58_400b"
        events_path = "events.jsonl"
        tracks_path = "tracks.jsonl"
        reports_dir = None
        evidence_index = None

    def fake_process(video_path, **kwargs):
        seen.update(kwargs)
        return FakeBatch()

    monkeypatch.setattr(capture_module, "fetch_capture", fake_fetch)
    monkeypatch.setattr(colab, "_process_one", fake_process)

    result = colab.activate(
        source="server_capture",
        video_dir=tmp_path,
        capture_seconds=30,
        device="cpu",
        webhook_token="pipeline-token",
        replay=False,
    )

    assert result.processed == 1
    assert result.source == "server_capture"
    assert seen["base_timestamp"] == "2026-11-09T10:00:00"
    assert seen["run_id"] == "cap_20261109_100000_abcdef"
