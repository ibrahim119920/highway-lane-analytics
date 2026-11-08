"""Tests for the webhook replay sender (no real HTTP)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lane_analytics.replay import (
    ReplayError,
    build_headers,
    check_health,
    endpoint,
    ensure_event_ids,
    group_windows,
    post_json,
    replay_run,
    resolve_base_url,
    resolve_token,
    wait_healthy,
)


def make_event(time_s: float, event_type: str = "lane_enter", track_id: int = 1) -> dict:
    return {
        "camera_id": "cam_01",
        "event_id": f"{track_id}-{time_s}",
        "event_type": event_type,
        "track_id": track_id,
        "lane_name": "nb_lane_1",
        "timestamp": "2026-11-07T23:25:41",
        "video_time_s": time_s,
        "frame_index": int(time_s * 18),
    }


def write_jsonl(path: Path, items: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, status_code: int = 202, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict | None]] = []
        self.health = FakeResponse(200, {"status": "ok"})

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, None))
        return self.health

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if not self.responses:
            return FakeResponse(202, {"accepted": len((json or {}).get("events", []))})
        return self.responses.pop(0)


def test_group_windows_splits_on_event_time():
    events = [make_event(t) for t in (0.0, 1.0, 4.9, 5.0, 5.1, 10.2)]
    batches = group_windows(events, window_seconds=5.0)
    assert [len(batch.events) for batch in batches] == [3, 2, 1]
    assert batches[0].offset_s == 0.0
    assert batches[1].offset_s == 5.0
    assert batches[2].offset_s == 10.2


def test_group_windows_preserves_order_for_equal_timestamps():
    events = [
        make_event(1.0, "lane_exit"),
        make_event(1.0, "lane_change"),
        make_event(1.0, "lane_enter"),
    ]
    batches = group_windows(events, window_seconds=5.0)
    assert [event["event_type"] for event in batches[0].events] == [
        "lane_exit",
        "lane_change",
        "lane_enter",
    ]


def test_group_windows_caps_batch_size():
    events = [make_event(t * 0.1) for t in range(25)]
    batches = group_windows(events, window_seconds=60.0, max_batch=10)
    assert [len(batch.events) for batch in batches] == [10, 10, 5]


def test_group_windows_can_start_at_video_zero():
    events = [make_event(20.0), make_event(21.0)]
    # Starting at video zero keeps the real 20 s lead-in.
    batches = group_windows(events, window_seconds=5.0, start_at_first_event=False)
    assert batches[0].offset_s == 20.0
    # Starting at the first event removes the idle lead-in.
    batches = group_windows(events, window_seconds=5.0, start_at_first_event=True)
    assert batches[0].offset_s == 0.0


def test_group_windows_rejects_bad_parameters():
    with pytest.raises(ReplayError, match="window_seconds"):
        group_windows([make_event(0.0)], window_seconds=0)
    with pytest.raises(ReplayError, match="max_batch"):
        group_windows([make_event(0.0)], max_batch=0)


def test_endpoint_accepts_base_or_full_webhook_url():
    assert endpoint("https://abc.ngrok-free.app", "/health") == "https://abc.ngrok-free.app/health"
    assert endpoint("https://abc.ngrok-free.app/", "/webhook") == "https://abc.ngrok-free.app/webhook"
    assert (
        endpoint("https://abc.ngrok-free.app/webhook", "/webhook/tracks")
        == "https://abc.ngrok-free.app/webhook/tracks"
    )


def test_build_headers_includes_auth_and_ngrok_hint():
    headers = build_headers("secret")
    assert headers["Authorization"] == "Bearer secret"
    assert headers["ngrok-skip-browser-warning"] == "true"
    assert "Authorization" not in build_headers("")


def test_post_json_retries_retryable_status():
    delays: list[float] = []
    session = FakeSession([FakeResponse(503), FakeResponse(202, {"accepted": 1})])
    response, retries = post_json(
        session,
        "http://test/webhook",
        {"events": []},
        headers={},
        timeout=1,
        retries=3,
        sleep=delays.append,
    )
    assert response.status_code == 202
    assert retries == 1
    assert len(delays) == 1


def test_post_json_raises_on_client_error():
    session = FakeSession([FakeResponse(400, {"error": "bad"})])
    with pytest.raises(ReplayError, match="400"):
        post_json(session, "http://test/webhook", {}, headers={}, timeout=1, retries=0)


def test_replay_dry_run_writes_batches(tmp_path: Path):
    events_path = write_jsonl(
        tmp_path / "run_a.jsonl",
        [make_event(0.0), make_event(1.0), make_event(6.0)],
    )
    tracks_path = write_jsonl(
        tmp_path / "tracks.jsonl",
        [{"camera_id": "cam_01", "track_id": 1, "lane_stints": []}],
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "total_vehicles": 1,
                "total_events": 3,
                "run": {"run_id": "run_a", "duration_s": 10},
            }
        ),
        encoding="utf-8",
    )

    result = replay_run(
        events_path,
        tracks_path=tracks_path,
        summary_path=summary_path,
        dry_run=True,
        dry_run_dir=tmp_path / "out",
        window_seconds=5.0,
        run_id="run_a",
    )

    target = tmp_path / "out" / "run_a"
    assert result.events_sent == 3
    assert result.event_batches == 2
    assert result.tracks_sent == 1
    assert result.run_sent is True
    assert (target / "events_0000.json").exists()
    assert (target / "events_0001.json").exists()
    assert (target / "tracks_0000.json").exists()
    assert (target / "run.json").exists()
    assert (target / "manifest.json").exists()
    run_body = json.loads((target / "run.json").read_text(encoding="utf-8"))
    assert run_body["complete"] is True
    assert run_body["run"]["run_id"] == "run_a"


def test_replay_posts_events_tracks_and_run(tmp_path: Path):
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [make_event(0.0), make_event(1.0)])
    tracks_path = write_jsonl(
        tmp_path / "tracks.jsonl", [{"camera_id": "cam_01", "track_id": 1}]
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"run": {"run_id": "run_a"}}), encoding="utf-8")

    session = FakeSession()
    result = replay_run(
        events_path,
        tracks_path=tracks_path,
        summary_path=summary_path,
        url="https://abc.ngrok-free.app/webhook",
        token="secret",
        run_id="run_a",
        session=session,
        sleep=lambda _: None,
    )

    urls = [url for url, _ in session.calls]
    assert urls[0] == "https://abc.ngrok-free.app/health"
    assert urls[1] == "https://abc.ngrok-free.app/webhook"
    assert urls[2] == "https://abc.ngrok-free.app/webhook/tracks"
    assert urls[3] == "https://abc.ngrok-free.app/webhook/run"
    assert result.events_sent == 2
    assert result.tracks_sent == 1
    assert result.run_sent is True
    assert session.calls[1][1]["run_id"] == "run_a"
    assert session.calls[1][1]["source"] == "kaggle-replay"
    assert session.calls[3][1]["complete"] is True


def test_replay_requires_url_when_not_dry_run(tmp_path: Path):
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [make_event(0.0)])
    with pytest.raises(ReplayError, match="webhook URL"):
        replay_run(events_path, url=None, dry_run=False, sleep=lambda _: None)


def test_replay_rejects_empty_camera(tmp_path: Path):
    events_path = write_jsonl(
        tmp_path / "run_a.jsonl", [{"event_type": "lane_enter"}]
    )
    with pytest.raises(ReplayError, match="camera_id"):
        replay_run(events_path, dry_run=True, dry_run_dir=tmp_path)


def test_post_json_retries_cloudflare_52x():
    delays: list[float] = []
    session = FakeSession([FakeResponse(522), FakeResponse(202, {"accepted": 1})])
    response, retries = post_json(
        session,
        "https://lane-api.example/webhook",
        {"events": []},
        headers={},
        timeout=1,
        retries=2,
        sleep=delays.append,
    )
    assert response.status_code == 202
    assert retries == 1
    assert len(delays) == 1


def test_ensure_event_ids_are_stable_without_an_existing_id():
    base = {
        "camera_id": "cam_01",
        "event_type": "lane_enter",
        "track_id": 7,
        "frame_index": 10,
        "timestamp": "2026-11-08T06:58:12",
        "video_time_s": 1.5,
    }
    first = ensure_event_ids([base], run_id="run_a", camera_id="cam_01")
    second = ensure_event_ids([base], run_id="run_a", camera_id="cam_01")
    assert first[0]["event_id"] == second[0]["event_id"]
    assert len(first[0]["event_id"]) == 32


def test_replay_rejects_timestamp_with_offset(tmp_path: Path):
    event = make_event(0.0)
    event["timestamp"] = "2026-11-08T06:58:12+07:00"
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [event])
    with pytest.raises(ReplayError, match="timezone offset"):
        replay_run(events_path, dry_run=True, dry_run_dir=tmp_path)


def test_replay_rejects_utc_timestamp(tmp_path: Path):
    event = make_event(0.0)
    event["timestamp"] = "2026-11-08T06:58:12Z"
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [event])
    with pytest.raises(ReplayError, match="not UTC"):
        replay_run(events_path, dry_run=True, dry_run_dir=tmp_path)


def test_replay_rejects_camera_mismatch(tmp_path: Path):
    events_path = write_jsonl(
        tmp_path / "run_a.jsonl",
        [make_event(0.0), {**make_event(1.0), "camera_id": "cam_02"}],
    )
    with pytest.raises(ReplayError, match="differs from envelope"):
        replay_run(events_path, dry_run=True, dry_run_dir=tmp_path)


def test_replay_rejects_partial_accepted_batch(tmp_path: Path):
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [make_event(0.0)])
    session = FakeSession([FakeResponse(202, {"accepted": 0, "rejected": 1})])
    with pytest.raises(ReplayError, match="rejected 1 item"):
        replay_run(
            events_path,
            url="https://lane-api.example",
            token="secret",
            run_id="run_a",
            session=session,
            sleep=lambda _: None,
        )


def test_replay_can_send_run_metadata_from_memory(tmp_path: Path):
    events_path = write_jsonl(tmp_path / "run_a.jsonl", [make_event(0.0)])
    session = FakeSession()
    result = replay_run(
        events_path,
        summary_data={"run": {"run_id": "run_a"}, "total_events": 1},
        config_data={"occupancy": {"assignment": "bbox_vote"}},
        url="https://lane-api.example",
        token="secret",
        run_id="run_a",
        session=session,
        sleep=lambda _: None,
    )
    assert result.run_sent is True
    assert session.calls[-1][0] == "https://lane-api.example/webhook/run"
    body = session.calls[-1][1]
    assert body["complete"] is True
    assert body["config"]["occupancy"]["assignment"] == "bbox_vote"


def test_wait_healthy_retries_until_sink_is_ready():
    class FlakySession(FakeSession):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def get(self, url, headers=None, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                return FakeResponse(200, {"healthy": True, "sink": {"status": "starting"}})
            return FakeResponse(200, {"healthy": True, "sink": {"status": "ok"}})

    delays: list[float] = []
    session = FlakySession()
    payload = wait_healthy(
        "https://lane-api.example",
        attempts=2,
        delay=0,
        session=session,
        sleep=delays.append,
    )
    assert payload["sink"]["status"] == "ok"
    assert session.attempts == 2
    assert len(delays) == 1


def test_check_health_rejects_unhealthy_payload():
    session = FakeSession()
    session.health = FakeResponse(200, {"healthy": False})
    with pytest.raises(ReplayError, match="unhealthy"):
        check_health("https://lane-api.example", session=session)


def test_resolve_base_url_and_token_from_environment(monkeypatch):
    monkeypatch.delenv("PIPELINE_BASE_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    assert resolve_base_url() == "https://lane-api.syaifudin-fam.my.id"
    monkeypatch.setenv("PIPELINE_BASE_URL", "https://staging.example/")
    assert resolve_base_url() == "https://staging.example"

    monkeypatch.delenv("PIPELINE_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_TOKEN", raising=False)
    assert resolve_token() is None
    monkeypatch.setenv("WEBHOOK_TOKEN", "legacy")
    assert resolve_token() == "legacy"
    monkeypatch.setenv("PIPELINE_WEBHOOK_TOKEN", "new")
    assert resolve_token() == "new"
