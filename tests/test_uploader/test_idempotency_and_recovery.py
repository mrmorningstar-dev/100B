"""Test suite for upload crash recovery, idempotency, quota exhaustion, and kill switch."""
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError

from uploader.youtube_client import YouTubeUploader
from uploader.db import UploadDatabase
from upload import cmd_upload


@pytest.fixture
def mock_youtube():
    return MagicMock()


@pytest.fixture
def mock_db(tmp_path):
    return UploadDatabase(tmp_path / "test.db")


@pytest.fixture
def sample_config():
    return {
        "upload": {
            "mode": "scheduled",
            "first_run_private": False,
            "max_uploads_per_day": 6,
        },
        "quota": {
            "daily_cap": 10000,
            "insert_cost": 1600,
            "list_cost": 1,
        },
    }


def test_idempotency_skips_already_uploaded_video(mock_youtube, mock_db, sample_config):
    uploader = YouTubeUploader(mock_youtube, config=sample_config, db=mock_db)

    # Pre-record uploaded post_id
    mock_db.record_attempt(
        post_id="p_existing",
        target_date="2026-11-20",
        slot_time="2026-11-20T14:00:00-05:00",
        status="uploaded",
        video_id="yt_already_done",
    )

    slot = {
        "post_id": "p_existing",
        "slot_time": "2026-11-20T14:00:00-05:00",
    }

    status, video_id, err = uploader.upload_slot(slot, target_date="2026-11-20")

    assert status == "uploaded"
    assert video_id == "yt_already_done"
    # videos.insert must NOT have been called!
    assert not mock_youtube.videos().insert.called


def test_crash_recovery_via_ref_marker_scan(mock_youtube, mock_db, sample_config):
    uploader = YouTubeUploader(mock_youtube, config=sample_config, db=mock_db)

    # Mock channels().list() to return uploads playlist ID
    mock_youtube.channels().list().execute.return_value = {
        "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU_test_channel"}}}]
    }

    # Mock playlistItems().list() to return recent video with [ref:p_crashed] marker in description
    mock_youtube.playlistItems().list().execute.return_value = {
        "items": [
            {
                "snippet": {
                    "title": "A funny clip #Shorts",
                    "description": "Hilarious clip credit: user1\n\n#Shorts\n\n[ref:p_crashed]",
                    "resourceId": {"videoId": "yt_recovered_777"},
                },
                "contentDetails": {"videoId": "yt_recovered_777"},
            }
        ]
    }

    slot = {
        "post_id": "p_crashed",
        "slot_time": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
    }

    status, video_id, err = uploader.upload_slot(slot, target_date="2026-11-20")

    assert status == "uploaded"
    assert video_id == "yt_recovered_777"
    assert not mock_youtube.videos().insert.called

    # DB should now contain the recovered video
    rec = mock_db.get_upload("p_crashed")
    assert rec is not None
    assert rec["video_id"] == "yt_recovered_777"


@patch("uploader.youtube_client.send_telegram_alert")
@patch("uploader.youtube_client.validate_video_and_metadata")
@patch("uploader.youtube_client.MediaFileUpload")
def test_quota_exceeded_fatal_stop(mock_media, mock_val, mock_alert, mock_youtube, mock_db, sample_config, tmp_path):
    uploader = YouTubeUploader(mock_youtube, config=sample_config, db=mock_db)

    mock_val.return_value = (True, "OK", {
        "title": "Title",
        "description": "Desc",
        "file_size": 1000,
    })

    # Mock channels to return empty so ref marker check doesn't find it
    mock_youtube.channels().list().execute.return_value = {"items": []}

    # Simulate HttpError 403 quotaExceeded
    mock_resp = MagicMock(status=403, reason="Forbidden")
    http_err = HttpError(resp=mock_resp, content=b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')

    mock_request = MagicMock()
    mock_request.next_chunk.side_effect = http_err
    mock_youtube.videos().insert.return_value = mock_request

    slot = {
        "post_id": "p_quota",
        "slot_time": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
        "video_path": str(tmp_path / "vid.mp4"),
    }

    status, video_id, err = uploader.upload_slot(slot, target_date="2026-11-20")

    assert status == "pending"
    assert err == "quotaExceeded"
    # Ensure alert was sent
    assert mock_alert.called
    assert mock_alert.call_args[0][0] == "QUOTA_EXCEEDED"


@patch("uploader.youtube_client.Path.exists")
def test_kill_switch_stop_file(mock_exists, mock_youtube, mock_db, sample_config):
    uploader = YouTubeUploader(mock_youtube, config=sample_config, db=mock_db)
    mock_exists.return_value = True  # Simulates STOP file present

    slot = {
        "post_id": "p_stop",
        "slot_time": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
    }

    status, video_id, err = uploader.upload_slot(slot, target_date="2026-11-20")

    assert status == "pending"
    assert "Kill switch" in err
    assert not mock_youtube.videos().insert.called


@patch("uploader.youtube_client.send_telegram_alert")
def test_max_uploads_per_day_enforced(mock_alert, mock_youtube, mock_db, sample_config):
    """Enforces max_uploads_per_day limit based on the Pacific-time day."""
    sample_config["upload"]["max_uploads_per_day"] = 2
    uploader = YouTubeUploader(mock_youtube, config=sample_config, db=mock_db)

    p_date = mock_db.get_pacific_date()

    # Pre-record 2 uploads completed today (Pacific time)
    mock_db.record_attempt(
        post_id="up1",
        target_date="2026-11-20",
        slot_time="2026-11-20T12:00:00-05:00",
        status="uploaded",
        video_id="yt_1",
        pacific_date=p_date,
    )
    mock_db.record_attempt(
        post_id="up2",
        target_date="2026-11-20",
        slot_time="2026-11-20T14:00:00-05:00",
        status="uploaded",
        video_id="yt_2",
        pacific_date=p_date,
    )
    assert mock_db.get_pacific_uploads_count(p_date) == 2

    # Attempt to upload 3rd video
    slot = {
        "post_id": "up3",
        "slot_time": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
    }

    status, video_id, err = uploader.upload_slot(slot, target_date="2026-11-20")

    assert status == "pending"
    assert "Daily upload limit reached for Pacific day" in err
    assert not mock_youtube.videos().insert.called
    assert mock_alert.called
    assert mock_alert.call_args[0][0] == "DAILY_UPLOAD_LIMIT_REACHED"


def test_cmd_upload_limit_flag(tmp_path):
    """Verifies that --limit N restricts cmd_upload to processing at most N fresh slots."""
    sched_dir = tmp_path / "agent" / "data" / "edited" / "2026-11-20"
    sched_dir.mkdir(parents=True, exist_ok=True)
    sched_file = sched_dir / "schedule.json"

    slots_data = [
        {"post_id": "slot_1", "slot_time": "2026-11-20T12:00:00-05:00", "title": "T1", "status": "queued"},
        {"post_id": "slot_2", "slot_time": "2026-11-20T14:00:00-05:00", "title": "T2", "status": "queued"},
        {"post_id": "slot_3", "slot_time": "2026-11-20T17:00:00-05:00", "title": "T3", "status": "queued"},
    ]
    with open(sched_file, "w", encoding="utf-8") as f:
        json.dump({"target_date": "2026-11-20", "slots": slots_data}, f)

    args = SimpleNamespace(
        date="2026-11-20",
        dry_run=True,
        confirm_public=False,
        mode="scheduled",
        limit=1,
    )

    with patch("upload.BASE_DIR", tmp_path), \
         patch("upload.UploadDatabase") as mock_db_cls, \
         patch("upload.YouTubeUploader") as mock_uploader_cls, \
         patch("upload.send_upload_summary"):

        mock_uploader = MagicMock()
        mock_uploader.upload_slot.return_value = ("uploaded", "mock_vid_1", None)
        mock_uploader.quota.used_today = 1
        mock_uploader_cls.return_value = mock_uploader

        mock_db = MagicMock()
        mock_db.get_upload.return_value = None
        mock_db_cls.return_value = mock_db

        cmd_upload(args)

        # upload_slot should only be called once because limit=1
        assert mock_uploader.upload_slot.call_count == 1


