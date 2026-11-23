import json
import sqlite3
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import dashboard.backend.db as backend_db


@pytest.fixture
def test_db_with_run_stats(tmp_path, monkeypatch):
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
            reject_reasons_json TEXT DEFAULT '{}',
            source_stats_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_reason TEXT,
            hybrid_score REAL,
            created_at TEXT
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
            status TEXT DEFAULT 'queued',
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
    # Insert run_stats for 2026-11-20
    conn.execute("""
        INSERT INTO run_stats (target_date, found, approved, selected, created_at)
        VALUES ('2026-11-20', 25, 8, 6, '2026-11-20T07:15:00+05:30')
    """)
    # Insert run_stats for 2026-11-21 (future test date that must be excluded)
    conn.execute("""
        INSERT INTO run_stats (target_date, found, approved, selected, created_at)
        VALUES ('2026-11-21', 50, 10, 6, '2026-11-21T07:15:00+05:30')
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_db, "DB_PATH", db_file)
    monkeypatch.setattr(backend_db, "WORKSPACE_DIR", tmp_path)
    return db_file


def test_get_agent_days_clock_frozen_at_2026_11_20_10_13_ist(test_db_with_run_stats, monkeypatch):
    # Clock frozen at 2026-11-20 10:13 IST
    tz_ist = ZoneInfo("Asia/Kolkata")
    frozen_ist = datetime(2026, 11, 20, 10, 13, 0, tzinfo=tz_ist)
    monkeypatch.setattr(backend_db, "dashboard_today", lambda now=None: "2026-11-20")

    days = backend_db.get_agent_days(num_days=7)
    assert len(days) == 7

    # Last item in the contiguous strip must be 2026-11-20
    last_item = days[-1]
    assert last_item["date"] == "2026-11-20"
    assert last_item["is_today"] is True
    # The 2026-11-20 run_stats row must be included
    assert last_item["found"] == 25
    assert last_item["approved"] == 8

    # 2026-11-21 must NOT be in the strip
    all_dates = [d["date"] for d in days]
    assert "2026-11-21" not in all_dates
    assert all_dates == [
        "2026-11-14", "2026-11-15", "2026-11-16",
        "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"
    ]


def test_get_agent_days_clock_frozen_at_2026_11_21_00_30_ist(test_db_with_run_stats, monkeypatch):
    # Clock frozen at 2026-11-21 00:30 IST -> today is 2026-11-21
    monkeypatch.setattr(backend_db, "dashboard_today", lambda now=None: "2026-11-21")

    days = backend_db.get_agent_days(num_days=7)
    assert len(days) == 7
    assert days[-1]["date"] == "2026-11-21"
    assert days[-1]["is_today"] is True


def test_pending_run_flag_when_no_data_for_today(tmp_path, monkeypatch):
    # Empty DB: no run data for today
    db_file = tmp_path / "empty_agent.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE run_stats (id INTEGER PRIMARY KEY, target_date TEXT, found INTEGER, approved INTEGER, selected INTEGER, reject_reasons_json TEXT, source_stats_json TEXT, created_at TEXT);")
    conn.execute("CREATE TABLE agent_decisions (id INTEGER PRIMARY KEY, target_date TEXT, post_id TEXT, status TEXT, decision_reason TEXT, hybrid_score REAL);")
    conn.execute("CREATE TABLE schedule (id INTEGER PRIMARY KEY, target_date TEXT, slot_hour INTEGER, slot_time TEXT, post_id TEXT, video_path TEXT, title TEXT, status TEXT);")
    conn.close()

    monkeypatch.setattr(backend_db, "DB_PATH", db_file)
    monkeypatch.setattr(backend_db, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(backend_db, "dashboard_today", lambda now=None: "2026-11-20")
    monkeypatch.setattr(backend_db, "is_pipeline_active", lambda: False)

    days = backend_db.get_agent_days(num_days=7)
    today_item = next(d for d in days if d["date"] == "2026-11-20")
    assert today_item["is_today"] is True
    assert today_item["found"] == 0
    assert today_item["approved"] == 0
    assert today_item["pending_run"] is True


def test_task_scheduler_parser_and_failure_code(monkeypatch):
    # Test handling of PowerShell JSON with LastTaskResult failure code (0x8007010B = 2147942667)
    sample_json = json.dumps({
        "TaskName": "100B Daily Video Bot",
        "NextRunTime": "/Date(1795177800000)/",
        "LastTaskResult": 2147942667,
    })

    class MockProc:
        returncode = 0
        stdout = sample_json
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MockProc())

    info = backend_db.get_task_scheduler_info()
    assert info["next_run_iso"] is not None
    assert "+05:30" in info["next_run_iso"]
    assert info["last_task_result"] == 2147942667
    assert "Daily task failed to start (0x8007010B)" in info["last_task_error"]


def test_task_scheduler_fallback_returns_future_07_00_ist(monkeypatch, tmp_path):
    # Simulate failed subprocess call to trigger fallback
    class FailedProc:
        returncode = 1
        stdout = ""
        stderr = "error"

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: FailedProc())
    monkeypatch.setattr(backend_db, "WORKSPACE_DIR", tmp_path)

    fallback_iso = backend_db.get_fallback_next_run_time()
    assert fallback_iso is not None
    assert "+05:30" in fallback_iso
    dt = datetime.fromisoformat(fallback_iso)
    assert dt.hour == 7
    assert dt.minute == 0
    # Must be in the future
    assert dt > datetime.now(ZoneInfo("Asia/Kolkata"))


def test_classify_task_result_only_flags_real_launch_failures():
    c = backend_db.classify_task_result
    assert c(None) is None
    assert c(0) is None
    assert c(1) is None                  # run_daily.py exited with 1: task started fine
    assert c(2) is None
    assert c(267009) is None             # 0x41301 task is running
    assert c(267011) is None             # 0x41303 task has not run yet
    assert c(2147942667) == "Daily task failed to start (0x8007010B)"
    assert c(3221225786) == "Daily task crashed (0xC000013A)"
    assert c(-2147024629) == "Daily task failed to start (0x8007010B)"  # signed form
