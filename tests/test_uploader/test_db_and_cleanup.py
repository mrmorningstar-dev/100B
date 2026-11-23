"""Test suite for decoupled database operations and media retention cleanup."""
import json
import sqlite3
import pytest
from pathlib import Path
from datetime import datetime, timedelta

from uploader.db import UploadDatabase, update_schedule_slot, atomic_write_json
from uploader.cleanup import archive_verified_slot, purge_old_media


def test_upload_database_crud(tmp_path):
    db_file = tmp_path / "test_agent.db"
    db = UploadDatabase(db_file)

    # 1. Record attempt
    db.record_attempt(
        post_id="p1",
        target_date="2026-11-20",
        slot_time="2026-11-20T12:00:00-05:00",
        status="uploading",
        privacy="private",
    )
    rec = db.get_upload("p1")
    assert rec is not None
    assert rec["status"] == "uploading"
    assert rec["attempts"] == 1

    # 2. Update status
    db.update_status("p1", status="uploaded", video_id="yt_123")
    rec2 = db.get_upload("p1")
    assert rec2["status"] == "uploaded"
    assert rec2["video_id"] == "yt_123"
    assert rec2["uploaded_at"] is not None

    # 3. Query count and list
    assert db.get_daily_upload_count("2026-11-20") == 1
    uploads = db.get_uploads_by_date("2026-11-20")
    assert len(uploads) == 1
    assert uploads[0]["post_id"] == "p1"


def test_atomic_schedule_update(tmp_path):
    sched_file = tmp_path / "schedule.json"
    initial_data = {
        "target_date": "2026-11-20",
        "slots": [
            {"post_id": "slot_1", "status": "queued"},
            {"post_id": "slot_2", "status": "queued"},
        ]
    }
    atomic_write_json(sched_file, initial_data)

    update_schedule_slot(sched_file, "slot_1", {"status": "scheduled", "video_id": "vid_xyz"})

    with open(sched_file, "r", encoding="utf-8") as f:
        updated = json.load(f)

    assert updated["slots"][0]["status"] == "scheduled"
    assert updated["slots"][0]["video_id"] == "vid_xyz"
    assert updated["slots"][1]["status"] == "queued"


def test_archive_verified_slot(tmp_path):
    video = tmp_path / "agent" / "data" / "edited" / "2026-11-20" / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"content")

    dest = archive_verified_slot(video, "2026-11-20", base_dir=tmp_path)
    assert dest.is_file()
    assert dest.parent.name == "2026-11-20"
    assert "published" in str(dest)
    assert not video.exists()


def test_purge_old_media_safety_rules(tmp_path):
    # Setup directories
    cutoff_old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    # 1. Old published directory (should be purged)
    old_dir = tmp_path / "agent" / "data" / "published" / cutoff_old
    old_dir.mkdir(parents=True, exist_ok=True)
    old_vid = old_dir / "old_clip.mp4"
    old_vid.write_bytes(b"x" * 1000)
    old_sched = old_dir / "schedule.json"
    old_sched.write_text('{"keep": true}', encoding="utf-8")

    # 2. Recent published directory (should NOT be purged)
    recent_dir = tmp_path / "agent" / "data" / "published" / recent
    recent_dir.mkdir(parents=True, exist_ok=True)
    recent_vid = recent_dir / "recent_clip.mp4"
    recent_vid.write_bytes(b"x" * 1000)

    # 3. Reserve pool directory (MUST NEVER BE TOUCHED)
    reserve_dir = tmp_path / "agent" / "data" / "reserve" / cutoff_old
    reserve_dir.mkdir(parents=True, exist_ok=True)
    reserve_vid = reserve_dir / "reserve_clip.mp4"
    reserve_vid.write_bytes(b"x" * 1000)

    # 4. Database file (MUST NEVER BE TOUCHED)
    db_file = tmp_path / "agent" / "data" / "agent.db"
    db_file.write_bytes(b"database")

    res = purge_old_media(retention_days=14, base_dir=tmp_path)

    # Assertions
    assert not old_vid.exists(), "Old video should be deleted"
    assert old_sched.exists(), "schedule.json should NEVER be deleted"
    assert recent_vid.exists(), "Recent video should be preserved"
    assert reserve_vid.exists(), "Reserve pool media must NEVER be deleted"
    assert db_file.exists(), "Database must NEVER be deleted"
    assert res["purged_files"] == 1


def test_purge_old_media_skips_available_reserve_pool_clips(tmp_path):
    cutoff_old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    old_edited_dir = tmp_path / "agent" / "data" / "edited" / cutoff_old
    old_edited_dir.mkdir(parents=True, exist_ok=True)

    # File 1: An old edited video that is NOT in the reserve pool (should be deleted)
    normal_vid = old_edited_dir / "normal_clip.mp4"
    normal_vid.write_bytes(b"normal")

    # File 2: An old edited video that IS in reserve_pool with status 'available' (MUST be preserved)
    reserve_vid = old_edited_dir / "reserve_clip.mp4"
    reserve_vid.write_bytes(b"reserve")

    # Set up test database with reserve_pool table
    db_file = tmp_path / "agent.db"
    db = UploadDatabase(db_file)
    with db._get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reserve_pool (
                post_id TEXT PRIMARY KEY,
                video_path TEXT,
                status TEXT
            );
        """)
        conn.execute(
            "INSERT INTO reserve_pool (post_id, video_path, status) VALUES (?, ?, ?);",
            ("res_1", str(reserve_vid), "available"),
        )
        conn.commit()

    # First purge: reserve_vid should be preserved, normal_vid purged
    res = purge_old_media(retention_days=14, base_dir=tmp_path, db=db)
    assert not normal_vid.exists(), "Normal old video should be purged"
    assert reserve_vid.exists(), "Available reserve pool clip must be preserved even if old"
    assert res["purged_files"] == 1

    # Now mark reserve clip as 'used'
    with db._get_connection() as conn:
        conn.execute("UPDATE reserve_pool SET status = 'used' WHERE post_id = 'res_1';")
        conn.commit()

    # Second purge: now reserve_vid is no longer available, so it should be purged
    res2 = purge_old_media(retention_days=14, base_dir=tmp_path, db=db)
    assert not reserve_vid.exists(), "Used reserve pool clip can now be purged"
    assert res2["purged_files"] == 1


def test_production_database_safety_guard():
    """
    Verify that initializing UploadDatabase or AgentDatabase without explicit path
    under pytest or testing NEVER touches the real production database (agent/data/agent.db),
    and is automatically redirected to a temporary test database.
    """
    from uploader.db import UploadDatabase
    from agent.core.db import AgentDatabase

    prod_db = (Path(__file__).resolve().parent.parent.parent / "agent" / "data" / "agent.db").resolve()

    # 1. UploadDatabase with no args
    up_db = UploadDatabase()
    assert up_db.db_path != prod_db
    assert up_db.db_path.exists()

    # 2. UploadDatabase explicitly passing prod_db path during test
    up_db_forced = UploadDatabase(prod_db)
    assert up_db_forced.db_path != prod_db

    # 3. AgentDatabase explicitly passing prod_db path during test
    agent_db_forced = AgentDatabase(prod_db)
    assert agent_db_forced.db_path != prod_db

