import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import run_daily


def test_run_daily_resolves_date_once_and_passes_explicit_date(tmp_path, monkeypatch):
    """
    Verify that when run_daily.py runs with --date ist-today (or --date tomorrow),
    it resolves the date once to an explicit 'YYYY-MM-DD' and passes that exact same
    explicit date to agent/curate.py, upload.py upload, and upload.py verify.
    """
    mock_db = tmp_path / "test_agent.db"
    mock_lock = tmp_path / "daily_run.lock"
    monkeypatch.setattr(run_daily, "LOCK_FILE", mock_lock)

    # Fake schedule.json so curate check succeeds
    test_target_date = "2026-11-20"
    fake_sched_dir = tmp_path / "agent" / "data" / "edited" / test_target_date
    fake_sched_dir.mkdir(parents=True, exist_ok=True)
    fake_sched_file = fake_sched_dir / "schedule.json"
    fake_sched_file.write_text('{"slots": [{"post_id": "p1"}, {"post_id": "p2"}]}', encoding="utf-8")
    monkeypatch.setattr(run_daily, "BASE_DIR", tmp_path)

    executed_commands = []

    def mock_run_step(name, cmd, timeout_s=3600):
        executed_commands.append((name, cmd))
        return True

    monkeypatch.setattr(run_daily, "run_step", mock_run_step)

    # Mock uploader config and alerts
    monkeypatch.setattr("upload.load_uploader_config", lambda: {"timezone": "America/New_York", "upload": {"max_uploads_per_day": 2}})
    monkeypatch.setattr("uploader.dates.resolve_date_arg", lambda d, tz, now=None: test_target_date)
    monkeypatch.setattr(run_daily, "send_telegram_alert", lambda *args, **kwargs: None)

    test_args = [
        "run_daily.py",
        "--date", "ist-today",
        "--db-path", str(mock_db),
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    with pytest.raises(SystemExit) as excinfo:
        run_daily.main()

    assert excinfo.value.code == 0

    # Verify executed steps
    assert len(executed_commands) >= 2

    # 1. Curation step: receives explicit target_date_str
    curate_step = next(c for c in executed_commands if c[0] == "Video Curation (LangGraph)")
    cmd_curate = curate_step[1]
    assert "--date" in cmd_curate
    date_idx_curate = cmd_curate.index("--date")
    assert cmd_curate[date_idx_curate + 1] == test_target_date

    # 2. Upload step: receives explicit target_date_str
    upload_step = next(c for c in executed_commands if c[0] == "YouTube Upload Batch")
    cmd_upload = upload_step[1]
    assert "--date" in cmd_upload
    date_idx_upload = cmd_upload.index("--date")
    assert cmd_upload[date_idx_upload + 1] == test_target_date

    # 3. Verify step: receives explicit target_date_str
    verify_step = next(c for c in executed_commands if c[0] == "YouTube Post-Upload Verification")
    cmd_verify = verify_step[1]
    assert "--date" in cmd_verify
    date_idx_verify = cmd_verify.index("--date")
    assert cmd_verify[date_idx_verify + 1] == test_target_date
