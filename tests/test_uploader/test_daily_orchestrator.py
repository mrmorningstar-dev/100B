"""Test suite for daily orchestrator lock management and execution flow."""
import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from run_daily import DailyLockManager, is_pid_alive, run_step


def test_lock_acquire_and_release(tmp_path):
    lock_file = tmp_path / "test_daily.lock"
    manager = DailyLockManager(lock_file)

    assert manager.acquire() is True
    assert lock_file.exists()

    content = json.loads(lock_file.read_text(encoding="utf-8"))
    assert content["pid"] == os.getpid()

    manager.release()
    assert not lock_file.exists()


@patch("run_daily.is_pid_alive")
def test_stale_lock_detection_replaces_dead_pid(mock_alive, tmp_path):
    lock_file = tmp_path / "stale.lock"
    # Write a dead PID into lock file
    lock_file.write_text(json.dumps({"pid": 999999, "started_at": "2026-11-10T00:00:00"}), encoding="utf-8")
    mock_alive.return_value = False  # Simulate dead process

    manager = DailyLockManager(lock_file)
    assert manager.acquire() is True

    # Lock file should now contain current PID
    new_content = json.loads(lock_file.read_text(encoding="utf-8"))
    assert new_content["pid"] == os.getpid()
    manager.release()


@patch("run_daily.is_pid_alive")
def test_lock_contention_blocks_when_active(mock_alive, tmp_path):
    lock_file = tmp_path / "active.lock"
    lock_file.write_text(json.dumps({"pid": 1234, "started_at": "2026-11-15T00:00:00"}), encoding="utf-8")
    mock_alive.return_value = True  # Process is alive

    manager = DailyLockManager(lock_file)
    assert manager.acquire() is False


@patch("subprocess.run")
def test_run_step_success_and_failure(mock_sub):
    mock_sub.return_value = MagicMock(returncode=0)
    assert run_step("Test Step", ["python", "--version"], timeout_s=10) is True

    mock_sub.return_value = MagicMock(returncode=1)
    assert run_step("Failing Step", ["python", "fail.py"], timeout_s=10) is False


@patch("run_daily.send_telegram_alert")
@patch("run_daily.run_step")
def test_main_curation_zero_clips_fails(mock_step, mock_alert, tmp_path):
    """When curation produces 0 clips, run_daily must alert and exit code 1 without touching production db."""
    import run_daily
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        mock_step.return_value = True  # curate.py exited 0 but no schedule.json created

        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()

        assert exc_info.value.code == 1
        assert mock_alert.called
        assert mock_alert.call_args[0][0] == "DAILY_RUN_FAILED"

        # Verify run was recorded to temporary db, NOT production, and finished as failed
        from uploader.db import UploadDatabase
        test_db = UploadDatabase(db_file)
        last_run = test_db.get_last_daily_run()
        assert last_run is not None
        assert last_run["status"] == "failed"


@patch("run_daily.send_telegram_alert")
@patch("run_daily.run_step")
def test_main_upload_step_failure_fails(mock_step, mock_alert, tmp_path):
    """When upload step fails (returncode != 0), run_daily must alert and exit code 1 without touching production db."""
    import run_daily
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    # Create schedule.json with 6 slots
    sched_dir = tmp_path / "agent" / "data" / "edited" / "2026-11-20"
    sched_dir.mkdir(parents=True, exist_ok=True)
    sched_file = sched_dir / "schedule.json"
    sched_file.write_text(json.dumps({"slots": [{"post_id": f"p{i}"} for i in range(6)]}), encoding="utf-8")

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        # Step 1 (curation) passes, Step 2 (upload) fails,
        # followed by Step 3 (verify) and Step 3.5 (claims-sync) which run even after upload failure
        mock_step.side_effect = [True, False, True, True]

        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()

        assert exc_info.value.code == 1
        assert mock_alert.called
        assert mock_alert.call_args[0][0] == "DAILY_RUN_FAILED"

        from uploader.db import UploadDatabase
        test_db = UploadDatabase(db_file)
        last_run = test_db.get_last_daily_run()
        assert last_run is not None
        assert last_run["status"] == "failed"


@patch("run_daily.send_telegram_alert")
@patch("run_daily.run_step")
def test_main_curation_fewer_than_6_clips_partial(mock_step, mock_alert, tmp_path):
    """When curation produces fewer than 6 clips (e.g. 3) and upload succeeds, run_daily ends as partial (code 2)."""
    import run_daily
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    # Create schedule.json with only 3 slots (< 6)
    sched_dir = tmp_path / "agent" / "data" / "edited" / "2026-11-20"
    sched_dir.mkdir(parents=True, exist_ok=True)
    sched_file = sched_dir / "schedule.json"
    sched_file.write_text(json.dumps({"slots": [{"post_id": f"p{i}"} for i in range(3)]}), encoding="utf-8")

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("run_daily.purge_old_media"), \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        mock_step.return_value = True  # Curation and Upload both return success

        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()

        assert exc_info.value.code == 2
        assert mock_alert.called
        assert mock_alert.call_args[0][0] == "DAILY_RUN_PARTIAL"

        from uploader.db import UploadDatabase
        test_db = UploadDatabase(db_file)
        last_run = test_db.get_last_daily_run()
        assert last_run is not None
        assert last_run["status"] == "partial"


@patch("run_daily.send_telegram_alert")
@patch("run_daily.run_step")
def test_main_full_success(mock_step, mock_alert, tmp_path):
    """When curation produces 6 clips and upload succeeds, run_daily ends with code 0."""
    import run_daily
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    # Create schedule.json with full 6 slots
    sched_dir = tmp_path / "agent" / "data" / "edited" / "2026-11-20"
    sched_dir.mkdir(parents=True, exist_ok=True)
    sched_file = sched_dir / "schedule.json"
    sched_file.write_text(json.dumps({"slots": [{"post_id": f"p{i}"} for i in range(6)]}), encoding="utf-8")

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("run_daily.purge_old_media"), \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        mock_step.return_value = True  # Curation and Upload both return success

        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()

        assert exc_info.value.code == 0
        assert mock_alert.called
        assert mock_alert.call_args[0][0] == "DAILY_RUN_COMPLETE"

        from uploader.db import UploadDatabase
        test_db = UploadDatabase(db_file)
        last_run = test_db.get_last_daily_run()
        assert last_run is not None
        assert last_run["status"] == "success"


def test_main_idempotency_already_complete_exits_0(tmp_path):
    """If target date already has 6 uploads, run_daily must exit 0 immediately."""
    import run_daily
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    from uploader.db import UploadDatabase
    test_db = UploadDatabase(db_file)
    with test_db._get_connection() as conn:
        for i in range(6):
            conn.execute(
                "INSERT INTO uploads (post_id, target_date, slot_time, status) VALUES (?, ?, ?, ?)",
                (f"p_done_{i}", "2026-11-20", f"2026-11-20T{12+i}:00:00Z", "uploaded")
            )
        conn.commit()

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()
        assert exc_info.value.code == 0


@patch("run_daily.send_telegram_alert")
@patch("run_daily.run_step")
def test_main_scraper_blocked_waiting_status_before_19_ist(mock_step, mock_alert, tmp_path):
    """When scraper is blocked and current IST hour is < 19, run_daily must record 'waiting' and exit 0."""
    import run_daily
    from zoneinfo import ZoneInfo
    from datetime import datetime as real_datetime
    lock_file = tmp_path / "daily.lock"
    db_file = tmp_path / "test_agent.db"

    # Simulate scrape result blocked
    cands_dir = tmp_path / "scraper" / "data" / "candidates"
    cands_dir.mkdir(parents=True, exist_ok=True)
    res_file = cands_dir / "2026-11-20_scrape_result.json"
    res_file.write_text(json.dumps({"status": "blocked", "blocked_until": "11:30 AM IST"}), encoding="utf-8")

    fake_now = real_datetime(2026, 11, 23, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("run_daily.BASE_DIR", tmp_path), \
         patch("run_daily.LOCK_FILE", lock_file), \
         patch("run_daily.datetime") as mock_dt, \
         patch("sys.argv", ["run_daily.py", "--date", "2026-11-20", "--db-path", str(db_file)]):
        mock_dt.now.side_effect = lambda tz=None: fake_now if tz else real_datetime.now()
        mock_step.return_value = True

        with pytest.raises(SystemExit) as exc_info:
            run_daily.main()

        assert exc_info.value.code == 0
        assert mock_alert.called
        assert mock_alert.call_args[0][0] == "DAILY_RUN_WAITING"

        from uploader.db import UploadDatabase
        test_db = UploadDatabase(db_file)
        last_run = test_db.get_last_daily_run()
        assert last_run is not None
        assert last_run["status"] == "waiting"
        assert "WAITING" in last_run["message"]

