import os
import json
import sqlite3
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import MagicMock, patch
from starlette.testclient import TestClient

from dashboard.backend.app import app
import dashboard.backend.db as backend_db
import dashboard.backend.routes_schedule as routes_sched
import dashboard.backend.routes_youtube as routes_yt
import dashboard.backend.thumbs as backend_thumbs
import dashboard.backend.youtube_api as yt_api
import dashboard.backend.security as sec


@pytest.fixture
def client():
    sec._FAILED_AUTH_LOG.clear()
    token = sec.get_configured_api_key()
    headers = {"x-dashboard-token": token} if token else {}
    return TestClient(app, headers=headers)


@pytest.fixture
def test_db_setup(tmp_path, monkeypatch):
    """Setup temporary test database with run_stats, schedule, uploads, daily_runs."""
    db_file = tmp_path / "agent.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE run_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,
            found INTEGER DEFAULT 62,
            dropped_prescreen INTEGER DEFAULT 10,
            dropped_duration INTEGER DEFAULT 5,
            downloaded INTEGER DEFAULT 52,
            dropped_filters INTEGER DEFAULT 11,
            judged INTEGER DEFAULT 41,
            rejected_judge INTEGER DEFAULT 29,
            borderline INTEGER DEFAULT 0,
            approved INTEGER DEFAULT 12,
            selected INTEGER DEFAULT 6,
            reserve_added INTEGER DEFAULT 6,
            reject_reasons_json TEXT DEFAULT '{"not_funny": 19, "non_english": 11, "watermark": 8, "duration": 7, "tv_film": 5, "other": 0}',
            source_stats_json TEXT DEFAULT '{"r/Unexpected": {"judged": 9, "accepted": 4}}',
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE daily_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            uploaded INTEGER DEFAULT 6,
            scheduled INTEGER DEFAULT 6,
            failed INTEGER DEFAULT 0,
            message TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,
            slot_hour INTEGER NOT NULL,
            slot_time TEXT NOT NULL,
            post_id TEXT NOT NULL,
            video_path TEXT NOT NULL,
            title TEXT,
            description TEXT,
            caption TEXT,
            source_url TEXT,
            uploader TEXT,
            status TEXT DEFAULT 'queued',
            rights_status TEXT DEFAULT 'unreviewed',
            rights_risk INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(target_date, slot_time)
        );
    """)
    conn.execute("""
        CREATE TABLE uploads (
            post_id TEXT PRIMARY KEY,
            target_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            status TEXT NOT NULL,
            video_id TEXT,
            attempts INTEGER DEFAULT 1,
            last_error TEXT,
            uploaded_at TEXT,
            privacy TEXT,
            publish_at TEXT,
            pacific_date TEXT,
            claim_status TEXT DEFAULT 'none',
            rights_status TEXT DEFAULT 'unreviewed',
            rights_risk INTEGER DEFAULT 0
        );
    """)
    conn.execute("""
        CREATE TABLE reserve_pool (
            post_id TEXT PRIMARY KEY,
            video_path TEXT NOT NULL,
            caption TEXT,
            title TEXT,
            description TEXT,
            uploader TEXT,
            post_url TEXT,
            score REAL,
            duration REAL,
            resolution TEXT,
            status TEXT DEFAULT 'available',
            added_at TEXT NOT NULL,
            used_at TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            file_hash TEXT,
            hybrid_score REAL,
            status TEXT NOT NULL,
            decision_reason TEXT,
            target_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rights_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            target_date TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            layers_json TEXT NOT NULL,
            risk_score INTEGER NOT NULL,
            decision TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT,
            post_id TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            claimant TEXT NOT NULL,
            asset_title TEXT,
            track_title TEXT,
            track_artist TEXT,
            found_in TEXT,
            action TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL DEFAULT 'manual',
            noted_at TEXT NOT NULL,
            detected_at TEXT,
            last_seen_at TEXT,
            released_at TEXT,
            message_id TEXT,
            affects_reach INTEGER DEFAULT 0,
            action_taken TEXT,
            source_name TEXT,
            uploader TEXT,
            phashes_json TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rights_blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            value TEXT NOT NULL,
            source_claim_id INTEGER REFERENCES claims(id),
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            provider TEXT NOT NULL,
            calls INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            UNIQUE(day, provider)
        );
    """)

    # Populate sample rows
    conn.execute("""
        INSERT INTO daily_runs (started_at, finished_at, status, uploaded, scheduled, failed, message)
        VALUES ('2026-11-14T18:00:00', '2026-11-14T18:02:00', 'success', 6, 6, 0, 'Completed successfully');
    """)
    conn.execute("""
        INSERT INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES ('2026-11-15', 12, '2026-11-15T12:00:00-05:00', 'clip1', 'clip1.mp4', 'Tractor fail', 'u1', 'scheduled', '2026-11-14T18:00:00');
    """)
    conn.execute("""
        INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('clip1', '2026-11-15', '2026-11-15T12:00:00-05:00', 'scheduled', 'yt_vid_1', '2026-11-14T18:01:00', 'private', '2026-11-15T12:00:00-05:00');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_db, "DB_PATH", db_file)
    return db_file


def test_health_check(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_host_header_security(client):
    # Host header that is not localhost or 127.0.0.1 must be rejected
    resp = client.get("/api/health", headers={"Host": "malicious-site.com"})
    assert resp.status_code == 403


def test_pipeline_status(client, test_db_setup):
    resp = client.get("/api/pipeline/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "last_run" in data
    assert data["last_run"]["status"] == "success"
    assert "next_run_time" in data


def test_pipeline_status_includes_reddit_and_waiting_info(client, tmp_path, monkeypatch):
    """Verify /api/pipeline/status returns reddit breaker status and next_attempt_at."""
    agent_db = tmp_path / "agent.db"
    conn = sqlite3.connect(str(agent_db))
    conn.execute("""
        CREATE TABLE daily_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            uploaded INTEGER DEFAULT 0,
            scheduled INTEGER DEFAULT 4,
            failed INTEGER DEFAULT 0,
            message TEXT,
            next_attempt_at TEXT
        );
    """)
    conn.execute("""
        INSERT INTO daily_runs (started_at, finished_at, status, uploaded, scheduled, failed, message, next_attempt_at)
        VALUES ('2026-11-23T07:00:00', '2026-11-23T07:04:00', 'waiting', 0, 4, 0, 'Waiting for breaker', '10:00 AM IST')
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_db, "DB_PATH", agent_db)

    resp = client.get("/api/pipeline/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_run"]["status"] == "waiting"
    assert data["last_run"]["next_attempt_at"] == "10:00 AM IST"
    assert "reddit" in data
    assert data["reddit"]["status"] in ("ok", "throttled", "blocked")
    assert "max_requests_per_day" in data["reddit"]


def test_agent_days(client, test_db_setup):
    resp = client.get("/api/agent/days?days=7")
    assert resp.status_code == 200
    days = resp.json()
    assert isinstance(days, list)
    assert len(days) > 0


def test_agent_day_detail(client, test_db_setup):
    resp = client.get("/api/agent/day/2026-11-15")
    assert resp.status_code == 200
    data = resp.json()
    assert "totals" in data
    assert "funnel" in data
    assert "lineup" in data
    assert "reasons" in data
    assert len(data["lineup"]) == 1
    assert data["lineup"][0]["post_id"] == "clip1"
    # Missing score should return null (None), not a made-up 8.0
    assert data["lineup"][0]["hybrid_score"] is None


def test_schedule_get(client, test_db_setup):
    resp = client.get("/api/schedule")
    assert resp.status_code == 200
    data = resp.json()
    assert "videos_per_day" in data
    assert "slots" in data
    assert "timezone" in data


def test_schedule_put_security_and_validation(client, tmp_path, monkeypatch):
    # Setup temp configs for testing
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("# Initial comment\ntarget_count: 6\npeak_hours:\n  - '12:00'\n", encoding="utf-8")
    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", cfg_file)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", cfg_file)

    # 1. Missing custom security header
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 3, "slots": ["12:00", "14:00", "16:00"]},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Missing security header" in resp.json()["detail"]

    # 2. Slot count mismatch
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 3, "slots": ["12:00", "14:00"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 400
    assert "Slot count" in resp.json()["detail"]

    # 3. Not snapping to 15 minutes
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 2, "slots": ["12:07", "14:00"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 400
    assert "15-minute increments" in resp.json()["detail"]

    # 4. Duplicate slots
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 2, "slots": ["12:00", "12:00"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 400
    assert "Duplicate slot time" in resp.json()["detail"]


def test_schedule_put_ruamel_roundtrip_comment_preservation(client, tmp_path, monkeypatch):
    agent_cfg = tmp_path / "agent_config.yaml"
    uploader_cfg = tmp_path / "uploader_config.yaml"

    initial_agent_yaml = (
        "# Peak publishing hours header comment\n"
        "timezone: 'America/New_York'\n"
        "target_count: 6  # inline comment on target_count\n"
        "# Another comment\n"
        "peak_hours:\n"
        "  - 12\n"
        "  - 14\n"
        "\n"
        "# Allowed sources and exploration targets (Reddit is primary; Twitter is experimental)\n"
        "allowed_sources:\n"
        "  twitter_enabled: false                # Explicitly disabled; Reddit is primary\n"
    )
    agent_cfg.write_text(initial_agent_yaml, encoding="utf-8")
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 6\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)
    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: False)

    # Valid update
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 3, "slots": ["12:00", "14:15", "17:30"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["videos_per_day"] == 3
    assert data["slots"] == ["12:00", "14:15", "17:30"]

    # Verify backup exists
    assert agent_cfg.with_suffix(".yaml.bak").exists()

    # Verify whole file comparison:
    updated_text = agent_cfg.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected_text = (
        "# Peak publishing hours header comment\n"
        "timezone: 'America/New_York'\n"
        "target_count: 3  # inline comment on target_count\n"
        "# Another comment\n"
        "peak_hours:\n"
        "  - 12:00\n"
        "  - 14:15\n"
        "  - 17:30\n"
        "\n"
        "# Allowed sources and exploration targets (Reddit is primary; Twitter is experimental)\n"
        "allowed_sources:\n"
        "  twitter_enabled: false                # Explicitly disabled; Reddit is primary\n"
    )
    assert updated_text == expected_text


def test_youtube_status_rules(client, test_db_setup, monkeypatch):
    mock_service = MagicMock()
    mock_service.videos().list().execute.return_value = {
        "items": [
            {
                "id": "yt_vid_1",
                "status": {
                    "privacyStatus": "private",
                    "publishAt": "2026-11-15T12:00:00-05:00",
                },
                "statistics": {
                    "viewCount": "42",
                    "likeCount": "5"
                }
            }
        ]
    }
    monkeypatch.setattr(yt_api, "get_youtube_service", lambda: mock_service)
    yt_api._VIDEOS_CACHE.clear()

    resp = client.get("/api/youtube/day/2026-11-15")
    assert resp.status_code == 200
    data = resp.json()
    assert "clips" in data
    assert len(data["clips"]) == 1
    clip = data["clips"][0]
    assert clip["post_id"] == "clip1"
    # Was inserted as scheduled
    assert clip["status"] in ("Scheduled", "Published")


def test_youtube_missing_deleted_video(client, test_db_setup, monkeypatch):
    # Mock youtube service returning empty items (video was deleted on YouTube)
    mock_service = MagicMock()
    mock_service.videos().list().execute.return_value = {
        "items": []
    }
    monkeypatch.setattr(yt_api, "get_youtube_service", lambda: mock_service)
    yt_api._VIDEOS_CACHE.clear()

    resp = client.get("/api/youtube/day/2026-11-15")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["clips"]) == 1
    clip = data["clips"][0]
    assert clip["post_id"] == "clip1"
    assert clip["status"] == "Missing on YouTube"
    assert "Missing on YouTube" in clip["status_detail"]
    assert data["published_count"] == 0
    assert data["scheduled_count"] == 0


def test_http_range_video_streaming(client, tmp_path, monkeypatch):
    # Create dummy 100-byte video file
    dummy_video = tmp_path / "test_stream.mp4"
    data_bytes = bytes([i % 256 for i in range(100)])
    dummy_video.write_bytes(data_bytes)

    monkeypatch.setattr(routes_yt, "resolve_video_path_for_post", lambda post_id: dummy_video if post_id == "stream_clip" else None)

    # 1. Full request (no Range header)
    resp = client.get("/api/video/stream_clip/stream")
    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert len(resp.content) == 100

    # 2. Range request: bytes 0-9 (first 10 bytes)
    resp_range = client.get("/api/video/stream_clip/stream", headers={"Range": "bytes=0-9"})
    assert resp_range.status_code == 206
    assert resp_range.headers["Content-Range"] == "bytes 0-9/100"
    assert resp_range.headers["Content-Length"] == "10"
    assert resp_range.content == data_bytes[0:10]

    # 3. Range request: bytes 50-
    resp_range2 = client.get("/api/video/stream_clip/stream", headers={"Range": "bytes=50-"})
    assert resp_range2.status_code == 206
    assert resp_range2.headers["Content-Range"] == "bytes 50-99/100"
    assert resp_range2.content == data_bytes[50:100]

    # 4. Range request with end past file size: bytes 50-200 -> clamped to 50-99
    resp_range3 = client.get("/api/video/stream_clip/stream", headers={"Range": "bytes=50-200"})
    assert resp_range3.status_code == 206
    assert resp_range3.headers["Content-Range"] == "bytes 50-99/100"
    assert resp_range3.headers["Content-Length"] == "50"
    assert resp_range3.content == data_bytes[50:100]

    # 5. Invalid post_id -> 404
    resp_404 = client.get("/api/video/nonexistent/stream")
    assert resp_404.status_code == 404


def test_thumbnail_missing_returns_placeholder(client, monkeypatch, tmp_path):
    # Missing thumbnail returns placeholder with 200 OK (no 404 console errors)
    monkeypatch.setattr(routes_yt, "get_or_generate_thumbnail", lambda post_id: None)
    dummy_placeholder = tmp_path / "placeholder.jpg"
    dummy_placeholder.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(routes_yt, "get_placeholder_thumbnail", lambda: dummy_placeholder)

    resp = client.get("/api/thumbs/missing_clip_123.jpg")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8\xff\xd9"


def test_mock_youtube_retiming(client, test_db_setup, tmp_path, monkeypatch):
    # Setup configs
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("target_count: 6\npeak_hours: ['12:00']\nupload:\n  max_uploads_per_day: 6\n", encoding="utf-8")
    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", cfg_file)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", cfg_file)

    # Mock manage scope
    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: True)

    # Mock youtube service
    mock_service = MagicMock()
    mock_service.videos().list().execute.return_value = {
        "items": [
            {
                "id": "yt_vid_1",
                "status": {
                    "privacyStatus": "private",
                    "publishAt": "2026-11-15T12:00:00Z",
                    "license": "youtube",
                    "embeddable": True,
                }
            }
        ]
    }
    monkeypatch.setattr(yt_api, "get_youtube_service", lambda: mock_service)
    monkeypatch.setattr(yt_api, "has_manage_scope", lambda: True)

    # Test updating publish time
    ret = yt_api.update_video_publish_time("yt_vid_1", "2026-11-15T14:30:00Z")
    assert ret is True

    # Verify videos().update was called with the full status object
    mock_service.videos().update.assert_called_once()
    call_args = mock_service.videos().update.call_args[1]
    assert call_args["part"] == "status"
    assert call_args["body"]["id"] == "yt_vid_1"
    assert call_args["body"]["status"]["publishAt"] == "2026-11-15T14:30:00Z"
    assert call_args["body"]["status"]["embeddable"] is True


def test_put_schedule_retiming_updates_local_data(client, test_db_setup, tmp_path, monkeypatch):
    """
    Test that successful YouTube videos.update during PUT /api/schedule
    updates local SQLite (schedule.slot_time, uploads.publish_at) and schedule.json.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    # 1. Setup configs
    agent_cfg = tmp_path / "agent_config.yaml"
    uploader_cfg = tmp_path / "uploader_config.yaml"
    agent_cfg.write_text("timezone: 'America/New_York'\ntarget_count: 2\npeak_hours: ['12:00', '14:00']\n", encoding="utf-8")
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 2\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)

    # 2. Insert test scheduled clips into test_db_setup for tomorrow
    conn = sqlite3.connect(str(test_db_setup))
    old_slot_1 = f"{tomorrow_str}T12:00:00-05:00"
    old_slot_2 = f"{tomorrow_str}T14:00:00-05:00"
    old_pub_1 = f"{tomorrow_str}T17:00:00Z"
    old_pub_2 = f"{tomorrow_str}T19:00:00Z"

    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 12, ?, 'retime_clip1', 'clip1.mp4', 'Clip 1', 'up1', 'scheduled', '2026-11-14T18:00:00');
    """, (tomorrow_str, old_slot_1))
    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 14, ?, 'retime_clip2', 'clip2.mp4', 'Clip 2', 'up2', 'scheduled', '2026-11-14T18:00:00');
    """, (tomorrow_str, old_slot_2))

    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('retime_clip1', ?, ?, 'scheduled', 'vid_retime_1', '2026-11-14T18:01:00', 'private', ?);
    """, (tomorrow_str, old_slot_1, old_pub_1))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('retime_clip2', ?, ?, 'scheduled', 'vid_retime_2', '2026-11-14T18:01:00', 'private', ?);
    """, (tomorrow_str, old_slot_2, old_pub_2))
    conn.commit()
    conn.close()

    # 3. Create schedule.json for tomorrow
    data_dir = tmp_path / "data"
    edited_dir = data_dir / "edited" / tomorrow_str
    edited_dir.mkdir(parents=True, exist_ok=True)
    sched_json_path = edited_dir / "schedule.json"
    sched_json_content = {
        "target_date": tomorrow_str,
        "slots": [
            {
                "slot_hour": 12,
                "slot_time": old_slot_1,
                "post_id": "retime_clip1",
                "publish_at": old_pub_1,
            },
            {
                "slot_hour": 14,
                "slot_time": old_slot_2,
                "post_id": "retime_clip2",
                "publish_at": old_pub_2,
            },
        ]
    }
    sched_json_path.write_text(json.dumps(sched_json_content, indent=2), encoding="utf-8")

    monkeypatch.setattr(routes_sched, "SCHEDULE_DATA_DIR", data_dir)
    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: True)

    # 4. Mock update_video_publish_time
    mock_retime = MagicMock(return_value=True)
    monkeypatch.setattr(routes_sched, "update_video_publish_time", mock_retime)

    # 5. Execute PUT /api/schedule with new slots: 13:00 and 17:30
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 2, "slots": ["13:00", "17:30"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "success"
    assert res_data["retimed_count"] == 2
    assert mock_retime.call_count == 2

    # 6. Verify SQLite schedule table was updated
    conn = sqlite3.connect(str(test_db_setup))
    rows_sched = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT post_id, slot_time, slot_hour FROM schedule WHERE target_date = ?",
            (tomorrow_str,)
        ).fetchall()
    }
    expected_slot_1 = f"{tomorrow_str}T13:00:00-05:00"
    expected_slot_2 = f"{tomorrow_str}T17:30:00-05:00"
    assert rows_sched["retime_clip1"] == (expected_slot_1, 13)
    assert rows_sched["retime_clip2"] == (expected_slot_2, 17)

    # 7. Verify SQLite uploads table was updated
    rows_uploads = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT post_id, slot_time, publish_at FROM uploads WHERE target_date = ?",
            (tomorrow_str,)
        ).fetchall()
    }
    # 13:00 -05:00 is 18:00 UTC, 17:30 -05:00 is 22:30 UTC
    expected_pub_1 = f"{tomorrow_str}T18:00:00Z"
    expected_pub_2 = f"{tomorrow_str}T22:30:00Z"
    assert rows_uploads["retime_clip1"] == (expected_slot_1, expected_pub_1)
    assert rows_uploads["retime_clip2"] == (expected_slot_2, expected_pub_2)
    conn.close()

    # 8. Verify schedule.json was updated
    updated_json = json.loads(sched_json_path.read_text(encoding="utf-8"))
    json_slots = {s["post_id"]: s for s in updated_json["slots"]}
    assert json_slots["retime_clip1"]["slot_time"] == expected_slot_1
    assert json_slots["retime_clip1"]["slot_hour"] == 13
    assert json_slots["retime_clip1"]["publish_at"] == expected_pub_1

    assert json_slots["retime_clip2"]["slot_time"] == expected_slot_2
    assert json_slots["retime_clip2"]["slot_hour"] == 17
    assert json_slots["retime_clip2"]["publish_at"] == expected_pub_2


def test_put_schedule_no_change_does_nothing(client, test_db_setup, tmp_path, monkeypatch):
    """A no-change save must do nothing: no YouTube calls and no config rewrite."""
    cfg_file = tmp_path / "agent_config.yaml"
    cfg_content = "# Preserved comment\ntimezone: 'America/New_York'\ntarget_count: 2\npeak_hours: ['12:00', '14:00']\n"
    cfg_file.write_text(cfg_content, encoding="utf-8")
    uploader_cfg = tmp_path / "uploader_config.yaml"
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 2\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", cfg_file)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)

    mock_retime = MagicMock(return_value=True)
    monkeypatch.setattr(routes_sched, "update_video_publish_time", mock_retime)

    # Save exact same slots and count
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 2, "slots": ["12:00", "14:00"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["status"] == "success"
    assert res["retimed_count"] == 0
    assert res["notice"] == "No changes to schedule."
    # YouTube API must NOT have been called
    assert mock_retime.call_count == 0
    # Config file must remain untouched
    assert cfg_file.read_text(encoding="utf-8") == cfg_content


def test_put_schedule_with_stale_rows_cleans_up_and_pairs_only_moved_clips(client, test_db_setup, tmp_path, monkeypatch):
    """
    Test that stale rows in schedule table (e.g. an extra 13:00 and 18:00 row)
    are cleaned up without causing unique-key collisions, and only clips that actually
    moved are retimed.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    # Config has 6 slots
    agent_cfg = tmp_path / "agent_config.yaml"
    uploader_cfg = tmp_path / "uploader_config.yaml"
    agent_cfg.write_text("timezone: 'America/New_York'\ntarget_count: 6\npeak_hours: ['12:00', '14:00', '17:00', '19:00', '20:00', '21:00']\n", encoding="utf-8")
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 6\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)

    conn = sqlite3.connect(str(test_db_setup))
    post_ids = [f"clip_{i}" for i in range(1, 7)]
    hours = [12, 14, 17, 19, 20, 21]

    # Insert 6 valid clips into uploads
    for pid, h in zip(post_ids, hours):
        st = f"{tomorrow_str}T{h:02d}:00:00-05:00"
        pub = f"{tomorrow_str}T{h+5:02d}:00:00Z"
        conn.execute("""
            INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
            VALUES (?, ?, ?, 'scheduled', ?, '2026-11-16T18:00:00', 'private', ?);
        """, (pid, tomorrow_str, st, f"yt_{pid}", pub))
        conn.execute("""
            INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
            VALUES (?, ?, ?, ?, 'p.mp4', 'Title', 'up', 'scheduled', '2026-11-16T18:00:00');
        """, (tomorrow_str, h, st, pid))

    # Insert STALE rows that caused previous failures:
    # 1. Duplicate row for clip_2 at 13:00
    stale_st_13 = f"{tomorrow_str}T13:00:00-05:00"
    conn.execute("""
        INSERT INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 13, ?, 'clip_2', 'p.mp4', 'Duplicate', 'up', 'scheduled', '2026-11-16T18:00:00');
    """, (tomorrow_str, stale_st_13))

    # 2. Orphaned row at 18:00
    stale_st_18 = f"{tomorrow_str}T18:00:00-05:00"
    conn.execute("""
        INSERT INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 18, ?, 'stale_orphan_clip', 'p.mp4', 'Orphan', 'up', 'scheduled', '2026-11-16T18:00:00');
    """, (tomorrow_str, stale_st_18))
    conn.commit()
    conn.close()

    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: True)
    mock_retime = MagicMock(return_value=True)
    monkeypatch.setattr(routes_sched, "update_video_publish_time", mock_retime)

    # Change ONLY the 3rd slot (17:00 -> 17:30). The other 5 slots are unchanged.
    new_slots = ["12:00", "14:00", "17:30", "19:00", "20:00", "21:00"]
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 6, "slots": new_slots},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["status"] == "success"
    # ONLY clip_3 moved! The other 5 clips stayed at their uploaded slots.
    assert res["retimed_count"] == 1
    assert mock_retime.call_count == 1
    assert mock_retime.call_args[0][0] == "yt_clip_3"

    # Verify stale rows in schedule were cleaned up and no unique collisions occurred
    conn = sqlite3.connect(str(test_db_setup))
    sched_rows = conn.execute(
        "SELECT post_id, slot_time, slot_hour FROM schedule WHERE target_date = ? ORDER BY slot_time",
        (tomorrow_str,)
    ).fetchall()
    conn.close()

    # Exactly 6 rows should exist now
    assert len(sched_rows) == 6
    pids = [r[0] for r in sched_rows]
    assert "stale_orphan_clip" not in pids
    # clip_2 must only appear once
    assert pids.count("clip_2") == 1
    # clip_3 must be at 17:30
    clip_3_row = next(r for r in sched_rows if r[0] == "clip_3")
    assert f"{tomorrow_str}T17:30:00-05:00" == clip_3_row[1]


def test_put_schedule_rollback_on_youtube_failure(client, test_db_setup, tmp_path, monkeypatch):
    """
    Test that if YouTube update fails for any clip, all previous YouTube updates
    and local SQLite/schedule.json data are safely rolled back.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    agent_cfg = tmp_path / "agent_config.yaml"
    uploader_cfg = tmp_path / "uploader_config.yaml"
    agent_cfg.write_text("timezone: 'America/New_York'\ntarget_count: 2\npeak_hours: ['12:00', '14:00']\n", encoding="utf-8")
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 2\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)

    old_slot_1 = f"{tomorrow_str}T12:00:00-05:00"
    old_slot_2 = f"{tomorrow_str}T14:00:00-05:00"
    old_pub_1 = f"{tomorrow_str}T17:00:00Z"
    old_pub_2 = f"{tomorrow_str}T19:00:00Z"

    conn = sqlite3.connect(str(test_db_setup))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('rb_clip_1', ?, ?, 'scheduled', 'yt_rb_1', '2026-11-16T18:00:00', 'private', ?);
    """, (tomorrow_str, old_slot_1, old_pub_1))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('rb_clip_2', ?, ?, 'scheduled', 'yt_rb_2', '2026-11-16T18:00:00', 'private', ?);
    """, (tomorrow_str, old_slot_2, old_pub_2))

    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 12, ?, 'rb_clip_1', 'p.mp4', 'Clip 1', 'up', 'scheduled', '2026-11-16T18:00:00');
    """, (tomorrow_str, old_slot_1))
    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 14, ?, 'rb_clip_2', 'p.mp4', 'Clip 2', 'up', 'scheduled', '2026-11-16T18:00:00');
    """, (tomorrow_str, old_slot_2))
    conn.commit()
    conn.close()

    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: True)

    # First video succeeds, second video fails
    call_log = []
    def mock_retime_fn(vid_id, publish_at):
        call_log.append((vid_id, publish_at))
        if vid_id == "yt_rb_2":
            return False
        return True

    monkeypatch.setattr(routes_sched, "update_video_publish_time", mock_retime_fn)

    # Request new slots: 13:00 and 15:00
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 2, "slots": ["13:00", "15:00"]},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 500
    assert "safely rolled back" in resp.json()["detail"]

    # Verify that the first video was reverted on YouTube
    revert_calls = [c for c in call_log if c[0] == "yt_rb_1" and c[1] == old_pub_1]
    assert len(revert_calls) == 1

    # Verify SQLite was rolled back to original times
    conn = sqlite3.connect(str(test_db_setup))
    sched_1 = conn.execute("SELECT slot_time, slot_hour FROM schedule WHERE post_id = 'rb_clip_1'").fetchone()
    sched_2 = conn.execute("SELECT slot_time, slot_hour FROM schedule WHERE post_id = 'rb_clip_2'").fetchone()
    up_1 = conn.execute("SELECT slot_time, publish_at FROM uploads WHERE post_id = 'rb_clip_1'").fetchone()
    up_2 = conn.execute("SELECT slot_time, publish_at FROM uploads WHERE post_id = 'rb_clip_2'").fetchone()
    conn.close()

    assert sched_1[0] == old_slot_1
    assert sched_1[1] == 12
    assert sched_2[0] == old_slot_2
    assert sched_2[1] == 14

    assert up_1[0] == old_slot_1
    assert up_1[1] == old_pub_1
    assert up_2[0] == old_slot_2
    assert up_2[1] == old_pub_2


def test_put_schedule_move_one_slot_retimes_only_that_clip(client, test_db_setup, tmp_path, monkeypatch):
    """
    Test moving ONLY one slot (e.g. 12:00 to 22:00) retimes ONLY that one clip.
    It must make exactly 1 YouTube call (12:00 clip to 22:00), leaving the other
    5 clips (14:00, 17:00, 19:00, 20:00, 21:00) untouched.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    # 1. Config with 6 slots
    agent_cfg = tmp_path / "agent_config.yaml"
    uploader_cfg = tmp_path / "uploader_config.yaml"
    agent_cfg.write_text(
        "timezone: 'America/New_York'\ntarget_count: 6\npeak_hours: ['12:00', '14:00', '17:00', '19:00', '20:00', '21:00']\n",
        encoding="utf-8"
    )
    uploader_cfg.write_text("upload:\n  max_uploads_per_day: 6\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", uploader_cfg)

    # 2. Insert 6 clips for tomorrow
    conn = sqlite3.connect(str(test_db_setup))
    hours = [12, 14, 17, 19, 20, 21]
    for h in hours:
        pid = f"clip_{h}"
        st = f"{tomorrow_str}T{h:02d}:00:00-05:00"
        pub = f"{tomorrow_str}T{h+5:02d}:00:00Z" if h + 5 < 24 else f"{tomorrow_str}T{h+5-24:02d}:00:00Z"
        conn.execute("""
            INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
            VALUES (?, ?, ?, 'scheduled', ?, '2026-11-16T18:00:00', 'private', ?);
        """, (pid, tomorrow_str, st, f"yt_{pid}", pub))
        conn.execute("""
            INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
            VALUES (?, ?, ?, ?, 'video.mp4', 'Title', 'uploader', 'scheduled', '2026-11-16T18:00:00');
        """, (tomorrow_str, h, st, pid))
    conn.commit()
    conn.close()

    monkeypatch.setattr(routes_sched, "has_manage_scope", lambda: True)
    mock_retime = MagicMock(return_value=True)
    monkeypatch.setattr(routes_sched, "update_video_publish_time", mock_retime)

    # Move ONLY the 12:00 slot to 22:00. The remaining 5 slots (14:00, 17:00, 19:00, 20:00, 21:00) are identical.
    new_slots = ["14:00", "17:00", "19:00", "20:00", "21:00", "22:00"]
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 6, "slots": new_slots},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["status"] == "success"
    # MUST retimed exactly 1 clip!
    assert res["retimed_count"] == 1

    # Exactly 1 YouTube call for the moved clip (yt_clip_12)
    assert mock_retime.call_count == 1
    assert mock_retime.call_args[0][0] == "yt_clip_12"

    # Verify SQLite state
    conn = sqlite3.connect(str(test_db_setup))
    sched_rows = dict(conn.execute(
        "SELECT post_id, slot_hour FROM schedule WHERE target_date = ?",
        (tomorrow_str,)
    ).fetchall())
    uploads_rows = dict(conn.execute(
        "SELECT post_id, slot_time FROM uploads WHERE target_date = ?",
        (tomorrow_str,)
    ).fetchall())
    conn.close()

    # clip_12 was retimed to 22:00
    assert sched_rows["clip_12"] == 22
    assert uploads_rows["clip_12"] == f"{tomorrow_str}T22:00:00-05:00"

    # All other 5 clips kept their original slots
    for h in [14, 17, 19, 20, 21]:
        assert sched_rows[f"clip_{h}"] == h
        assert uploads_rows[f"clip_{h}"] == f"{tomorrow_str}T{h:02d}:00:00-05:00"


def test_put_schedule_preserves_all_comments_and_blank_lines(client, test_db_setup, tmp_path, monkeypatch):
    """
    Test that updating schedule with PUT /api/schedule modifies peak_hours in place
    and preserves ALL comments and blank lines throughout agent/config.yaml,
    specifically including '# Allowed sources and exploration targets (Reddit is primary; Twitter is experimental)'
    and the blank line before it. Compares the whole file.
    """
    real_agent_config = backend_db.WORKSPACE_DIR / "agent" / "config.yaml"
    original_content = real_agent_config.read_text(encoding="utf-8")

    test_agent_cfg = tmp_path / "agent_config.yaml"
    test_agent_cfg.write_text(original_content, encoding="utf-8")

    test_uploader_cfg = tmp_path / "uploader_config.yaml"
    test_uploader_cfg.write_text("upload:\n  max_uploads_per_day: 6\n", encoding="utf-8")

    monkeypatch.setattr(routes_sched, "AGENT_CONFIG_PATH", test_agent_cfg)
    monkeypatch.setattr(routes_sched, "UPLOADER_CONFIG_PATH", test_uploader_cfg)

    # PUT /api/schedule with new slots: change 12:00 to 22:00
    new_slots = ["14:00", "17:00", "19:00", "20:00", "21:00", "22:00"]
    resp = client.put(
        "/api/schedule",
        json={"videos_per_day": 6, "slots": new_slots},
        headers={"Content-Type": "application/json", "X-Dashboard-Request": "1"},
    )
    assert resp.status_code == 200

    # Read saved file content and compare whole file
    saved_content = test_agent_cfg.read_text(encoding="utf-8")

    expected_comment = "# Allowed sources and exploration targets (Reddit is primary; Twitter is experimental)"
    assert expected_comment in saved_content

    # Normalize line endings
    normalized_saved = saved_content.replace("\r\n", "\n")
    expected_block = (
        "  - 22:00\n\n"
        "# Allowed sources and exploration targets (Reddit is primary; Twitter is experimental)\n"
        "reddit:"
    )
    assert expected_block in normalized_saved

    # Strict whole-file comparison: character for character, only peak_hours entries changed
    norm_orig = original_content.replace("\r\n", "\n")
    expected_full_file = norm_orig.replace(
        """peak_hours:
  - 12
  - 14
  - 17
  - 19
  - 20
  - 21""",
        """peak_hours:
  - 14:00
  - 17:00
  - 19:00
  - 20:00
  - 21:00
  - 22:00""",
    )
    assert normalized_saved == expected_full_file


def test_compare_whole_file_after_saving_schedule(client, test_db_setup, tmp_path, monkeypatch):
    """
    Explicit test comparing the whole agent/config.yaml file line by line
    and character by character after saving schedule.
    """
    test_put_schedule_preserves_all_comments_and_blank_lines(client, test_db_setup, tmp_path, monkeypatch)


# ==============================================================================
# Phase 6 Rights Check & Claims API Tests
# ==============================================================================

def test_rights_status_api(client, test_db_setup):
    resp = client.get("/api/rights/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "is_local_only" in data
    assert "unreviewed_policy" in data
    assert "active_layers" in data
    assert "providers" in data
    assert "shazam" in data["providers"]
    assert "google_vision" in data["providers"]
    assert "errors_today" in data["providers"]["shazam"]
    assert "stats" in data


def test_rights_status_api_reports_shazam_failures(client, test_db_setup, monkeypatch):
    """Test that mostly-failing Shazam requests set provider status to error."""
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(test_db_setup))
    conn.execute("""
        INSERT OR REPLACE INTO provider_usage (day, provider, calls, errors)
        VALUES (?, 'shazam', 13, 13)
    """, (today_str,))
    conn.commit()
    conn.close()

    resp = client.get("/api/rights/status")
    assert resp.status_code == 200
    data = resp.json()
    audd_prov = data["providers"]["shazam"]
    assert audd_prov["status"] == "error"
    assert audd_prov["errors_today"] == 13
    assert audd_prov["error_message"] == "Shazam: most recent requests failed"
    assert data["active_layers"]["layer2b_fingerprint"] is False


def test_rights_review_queue_api(client, test_db_setup, monkeypatch):
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).strftime("%Y-%m-%d")

    # Mock fetch_live_video_stats
    def mock_fetch_live(video_ids):
        res = {}
        for vid in video_ids:
            if vid == "yt_today_priv":
                res[vid] = {
                    "found": True,
                    "missing": False,
                    "status": {"privacyStatus": "private"},
                    "statistics": {"viewCount": "0"},
                }
            elif vid == "yt_today_pub":
                res[vid] = {
                    "found": True,
                    "missing": False,
                    "status": {"privacyStatus": "public"},
                    "statistics": {"viewCount": "10"},
                }
        return res

    import dashboard.backend.routes_rights as routes_rights
    monkeypatch.setattr(routes_rights, "fetch_live_video_stats", mock_fetch_live, raising=False)
    import dashboard.backend.youtube_api as yt_api
    monkeypatch.setattr(yt_api, "fetch_live_video_stats", mock_fetch_live, raising=False)

    # Insert test clips for today (private scheduled) and tomorrow (scheduled)
    now_iso = "2026-11-17T01:00:00"
    conn = sqlite3.connect(str(test_db_setup))
    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 12, ?, 'clip_today_sched', 'video1.mp4', 'Today Scheduled', 'user1', 'scheduled', ?)
    """, (today_str, f"{today_str}T12:00:00-05:00", now_iso))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy, publish_at)
        VALUES ('clip_today_sched', ?, ?, 'scheduled', 'yt_today_priv', ?, 'private', ?)
    """, (today_str, f"{today_str}T12:00:00-05:00", now_iso, f"{today_str}T17:00:00Z"))

    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 14, ?, 'clip_tmrw_sched', 'video2.mp4', 'Tomorrow Scheduled', 'user2', 'scheduled', ?)
    """, (tomorrow_str, f"{tomorrow_str}T14:00:00-05:00", now_iso))

    # Also insert an already-public clip for today (should be excluded from review queue)
    conn.execute("""
        INSERT OR REPLACE INTO schedule (target_date, slot_hour, slot_time, post_id, video_path, title, uploader, status, created_at)
        VALUES (?, 9, ?, 'clip_today_pub', 'video3.mp4', 'Today Public', 'user3', 'scheduled', ?)
    """, (today_str, f"{today_str}T09:00:00-05:00", now_iso))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('clip_today_pub', ?, ?, 'uploaded', 'yt_today_pub', ?, 'public')
    """, (today_str, f"{today_str}T09:00:00-05:00", now_iso))
    conn.commit()
    conn.close()

    resp = client.get("/api/rights/review")
    assert resp.status_code == 200
    data = resp.json()
    assert "target_date" in data
    assert "clips" in data
    assert isinstance(data["clips"], list)

    pids = [c["post_id"] for c in data["clips"]]
    # Both today's private scheduled clip and tomorrow's clip must be in Needs Review
    assert "clip_today_sched" in pids
    assert "clip_tmrw_sched" in pids
    # Public clip must NOT be in Needs Review
    assert "clip_today_pub" not in pids


def test_rights_mark_clear_and_held_api(client, test_db_setup):
    headers = {"Content-Type": "application/json", "X-Dashboard-Request": "1"}
    # Test hold
    resp_hold = client.post("/api/rights/hold", json={"post_id": "clip1"}, headers=headers)
    assert resp_hold.status_code == 200
    data_hold = resp_hold.json()
    assert data_hold["success"] is True

    # Test mark-clear
    resp_clear = client.post("/api/rights/mark-clear", json={"post_id": "clip1"}, headers=headers)
    assert resp_clear.status_code == 200
    data_clear = resp_clear.json()
    assert data_clear["success"] is True


def test_rights_mark_claimed_and_blocklist_flow(client, test_db_setup):
    headers = {"Content-Type": "application/json", "X-Dashboard-Request": "1"}
    # 1. Register a claim with suggested blocklist items
    claim_payload = {
        "post_id": "clip1",
        "video_id": "yt_vid_1",
        "claimant": "CollabDRM",
        "claim_type": "visual",
        "asset_title": "Collab ViralHog - Test Video",
        "action": "held",
        "uploader": "risky_uploader",
        "source_name": "r/Unexpected",
        "notes": "Test claim entry",
    }
    resp = client.post("/api/rights/mark-claimed", json=claim_payload, headers=headers)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["success"] is True
    assert "claim_id" in res_data
    assert len(res_data["suggestions"]) > 0

    # 2. Query blocklist and verify pending suggestions
    resp_bl = client.get("/api/rights/blocklist")
    assert resp_bl.status_code == 200
    bl_data = resp_bl.json()
    assert "active" in bl_data
    assert "pending" in bl_data
    assert len(bl_data["pending"]) > 0

    pending_id = bl_data["pending"][0]["id"]

    # 3. Approve suggestion into active blocklist
    resp_action = client.post(f"/api/rights/blocklist/{pending_id}/approve", headers=headers)
    assert resp_action.status_code == 200
    assert resp_action.json()["success"] is True

    # 4. Verify it moved to active list
    resp_bl_after = client.get("/api/rights/blocklist")
    active_ids = [e["id"] for e in resp_bl_after.json()["active"]]
    assert pending_id in active_ids


def test_rights_write_security_guard(client, test_db_setup):
    """
    Verify security rules for rights write routes:
    - Missing X-Dashboard-Request returns 400
    - Non-JSON content-type returns 415
    - Disallowed Host header returns 403
    - Correct headers pass through
    """
    write_endpoints = [
        ("POST", "/api/rights/mark-clear", {"post_id": "clip1"}),
        ("POST", "/api/rights/hold", {"post_id": "clip1"}),
        ("POST", "/api/rights/action-published", {"post_id": "clip1", "action": "keep"}),
        ("POST", "/api/rights/blocklist/add", {"type": "text", "value": "test_agency"}),
        ("POST", "/api/rights/blocklist/1/toggle", None),
    ]

    for method, endpoint, payload in write_endpoints:
        # 1. Missing custom header -> 400
        resp_no_header = client.request(
            method,
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        assert resp_no_header.status_code == 400, f"{endpoint} should return 400 when missing header"
        assert "Missing security header" in resp_no_header.json()["detail"]

        # 2. Non-JSON content type -> 415
        resp_bad_ct = client.request(
            method,
            endpoint,
            content="post_id=clip1",
            headers={"Content-Type": "text/plain", "X-Dashboard-Request": "1"},
        )
        assert resp_bad_ct.status_code == 415, f"{endpoint} should return 415 for text/plain"

        # 3. Disallowed Host header -> 403
        resp_bad_host = client.request(
            method,
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json", "X-Dashboard-Request": "1", "Host": "evil-origin.com"},
        )
        assert resp_bad_host.status_code == 403, f"{endpoint} should return 403 for external host"


def test_rights_claims_history_and_recent_published(client, test_db_setup, monkeypatch):
    # 1. Claims history
    resp_hist = client.get("/api/rights/claims")
    assert resp_hist.status_code == 200
    hist_data = resp_hist.json()
    assert isinstance(hist_data, list)

    # 2. Setup mock live video stats
    def mock_fetch_live(video_ids):
        res = {}
        for vid in video_ids:
            if vid == "vid_live_public":
                res[vid] = {
                    "found": True,
                    "missing": False,
                    "status": {"privacyStatus": "public"},
                    "statistics": {"viewCount": "42", "likeCount": "5"},
                }
            elif vid == "vid_live_missing":
                res[vid] = {
                    "found": False,
                    "missing": True,
                    "status": {},
                    "statistics": {},
                }
            elif vid == "vid_live_private":
                res[vid] = {
                    "found": True,
                    "missing": False,
                    "status": {"privacyStatus": "private"},
                    "statistics": {"viewCount": "0", "likeCount": "0"},
                }
        return res

    import dashboard.backend.routes_rights as routes_rights
    monkeypatch.setattr(routes_rights, "fetch_live_video_stats", mock_fetch_live, raising=False)
    import dashboard.backend.youtube_api as yt_api
    monkeypatch.setattr(yt_api, "fetch_live_video_stats", mock_fetch_live, raising=False)

    # Insert test clips
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(test_db_setup))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('test_pub_1', ?, '12:00:00', 'uploaded', 'vid_live_public', '2026-11-16T12:00:00', 'private')
    """, (today_str,))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('test_miss_1', ?, '14:00:00', 'scheduled', 'vid_live_missing', '2026-11-16T14:00:00', 'private')
    """, (today_str,))
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('test_priv_1', ?, '16:00:00', 'scheduled', 'vid_live_private', '2026-11-16T16:00:00', 'private')
    """, (today_str,))
    conn.commit()
    conn.close()

    # 3. Query Recent published
    resp_recent = client.get("/api/rights/published")
    assert resp_recent.status_code == 200
    recent_data = resp_recent.json()
    assert isinstance(recent_data, list)

    items_by_pid = {r["post_id"]: r for r in recent_data}

    # Verify public live status overrides DB 'private'
    pub_item = items_by_pid.get("test_pub_1")
    assert pub_item is not None
    assert pub_item["privacy"] == "public"
    assert pub_item["live_privacy"] == "public"
    assert pub_item["live_status"] == "published"
    assert pub_item["is_missing"] is False
    assert pub_item["views"] == 42

    # Verify missing live status sets is_missing and privacy="missing"
    miss_item = items_by_pid.get("test_miss_1")
    assert miss_item is not None
    assert miss_item["privacy"] == "missing"
    assert miss_item["live_privacy"] == "missing"
    assert miss_item["live_status"] == "missing"
    assert miss_item["is_missing"] is True

    # Verify private live status
    priv_item = items_by_pid.get("test_priv_1")
    assert priv_item is not None
    assert priv_item["privacy"] == "private"
    assert priv_item["live_privacy"] == "private"
    assert priv_item["is_missing"] is False


def test_rights_action_published_remove_record_and_delete(client, test_db_setup, monkeypatch):
    """Verify that 'remove_record' and 'delete' properly purge records from uploads and schedule."""
    conn = sqlite3.connect(str(test_db_setup))
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Insert a missing clip and a normal published clip
    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('clip_to_remove', ?, '12:00:00', 'deleted', 'vid_missing_123', '2026-11-16T12:00:00', 'private')
    """, (today_str,))
    conn.execute("""
        INSERT OR REPLACE INTO schedule (post_id, target_date, slot_time, slot_hour, video_path, title, status, created_at)
        VALUES ('clip_to_remove', ?, '12:00:00', 12, 'dummy.mp4', 'Missing Video Title', 'deleted', '2026-11-16T12:00:00')
    """, (today_str,))

    conn.execute("""
        INSERT OR REPLACE INTO uploads (post_id, target_date, slot_time, status, video_id, uploaded_at, privacy)
        VALUES ('clip_to_delete', ?, '14:00:00', 'uploaded', 'vid_active_456', '2026-11-16T14:00:00', 'public')
    """, (today_str,))
    conn.execute("""
        INSERT OR REPLACE INTO schedule (post_id, target_date, slot_time, slot_hour, video_path, title, status, created_at)
        VALUES ('clip_to_delete', ?, '14:00:00', 14, 'dummy2.mp4', 'Active Video Title', 'uploaded', '2026-11-16T14:00:00')
    """, (today_str,))
    conn.commit()
    conn.close()

    headers = {"Content-Type": "application/json", "X-Dashboard-Request": "1"}

    # 1. Failure without DELETE confirmation
    resp_unconfirmed = client.post(
        "/api/rights/action-published",
        json={"post_id": "clip_to_remove", "video_id": "vid_missing_123", "action": "remove_record", "delete_confirmation": "NO"},
        headers=headers,
    )
    assert resp_unconfirmed.status_code == 400
    assert "Must type 'DELETE'" in resp_unconfirmed.json()["detail"]

    # 2. Successfully remove missing record with 'remove_record'
    resp_remove = client.post(
        "/api/rights/action-published",
        json={"post_id": "clip_to_remove", "video_id": "vid_missing_123", "action": "remove_record", "delete_confirmation": "DELETE"},
        headers=headers,
    )
    assert resp_remove.status_code == 200
    assert resp_remove.json()["success"] is True

    # 3. Successfully delete active video with 'delete'
    resp_delete = client.post(
        "/api/rights/action-published",
        json={"post_id": "clip_to_delete", "video_id": "vid_active_456", "action": "delete", "delete_confirmation": "DELETE"},
        headers=headers,
    )
    assert resp_delete.status_code == 200
    assert resp_delete.json()["success"] is True

    # 4. Verify neither clip appears in /api/rights/published
    resp_recent = client.get("/api/rights/published")
    assert resp_recent.status_code == 200
    recent_pids = [r["post_id"] for r in resp_recent.json()]
    assert "clip_to_remove" not in recent_pids
    assert "clip_to_delete" not in recent_pids


def test_schedule_and_pipeline_status_preserves_et_fields_and_utc_instants(client, test_db_setup):
    """
    Verify /api/schedule and /api/pipeline/status:
    1. Core scheduling stays strictly in ET: timezone="America/New_York".
    2. Display timezone is provided as Asia/Kolkata (IST).
    3. Slots remain in ET format (HH:MM).
    4. Next run time and schedule timestamps remain valid ISO instants.
    """
    # 1. Check /api/schedule
    resp_sched = client.get("/api/schedule")
    assert resp_sched.status_code == 200
    sched_data = resp_sched.json()

    assert sched_data["timezone"] == "America/New_York"
    assert sched_data.get("display_timezone") == "Asia/Kolkata"
    assert isinstance(sched_data["slots"], list)
    assert len(sched_data["slots"]) > 0
    # Every slot must be an ET HH:MM representation
    for s in sched_data["slots"]:
        assert ":" in s
        h, m = s.split(":")
        assert 0 <= int(h) <= 23
        assert 0 <= int(m) <= 59

    # 2. Check /api/pipeline/status
    resp_pipe = client.get("/api/pipeline/status")
    assert resp_pipe.status_code == 200
    pipe_data = resp_pipe.json()

    assert pipe_data.get("channel_timezone") == "America/New_York"
    assert pipe_data.get("display_timezone") == "Asia/Kolkata"
    assert "next_run_time" in pipe_data
    assert pipe_data["next_run_time"] is not None
    # Must be parseable as a valid ISO instant
    dt = datetime.fromisoformat(pipe_data["next_run_time"])
    assert dt is not None

