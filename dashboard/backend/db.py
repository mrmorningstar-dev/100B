import os
import re
import time
import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

# Decoupled database accessor: plain sqlite3, mode=ro (read-only), NO core imports
WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = WORKSPACE_DIR / "agent" / "data" / "agent.db"


def get_ro_connection() -> sqlite3.Connection:
    """Return a read-only SQLite connection using SQLite URI mode."""
    db_file = DB_PATH.resolve()
    if not db_file.is_file():
        # If DB file doesn't exist yet, connect to in-memory or fallback
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn
    # Windows-safe file URI: file:///C:/path/agent.db?mode=ro
    uri = f"file:///{db_file.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_rw_connection(timeout: float = 30.0) -> sqlite3.Connection:
    """Return a controlled read-write SQLite connection with row factory."""
    db_file = DB_PATH.resolve()
    conn = sqlite3.connect(str(db_file), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def update_local_retimed_data(
    target_date: str,
    retimed_items: List[Dict[str, Any]],
    db_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """
    Controlled write of retimed clip times to SQLite (schedule and uploads tables)
    and schedule.json files in an explicit transaction BEFORE YouTube updates.
    """
    if not retimed_items:
        return {"db_updated": 0, "json_updated": 0}

    actual_db_path = Path(db_path or DB_PATH).resolve()
    actual_data_dir = Path(data_dir or (WORKSPACE_DIR / "agent" / "data")).resolve()
    db_updated = 0

    if actual_db_path.is_file():
        conn = sqlite3.connect(str(actual_db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                # 1. Defensively clean up any orphaned schedule rows for target_date that do NOT belong to scheduled clips
                conn.execute("""
                    DELETE FROM schedule
                    WHERE target_date = ?
                      AND (post_id IS NULL OR post_id NOT IN (
                          SELECT post_id FROM uploads WHERE target_date = ?
                      ))
                """, (target_date, target_date))

                # Deduplicate if duplicate rows exist for the same post_id in schedule
                conn.execute("""
                    DELETE FROM schedule
                    WHERE target_date = ? AND id NOT IN (
                        SELECT MAX(id) FROM schedule WHERE target_date = ? GROUP BY post_id
                    )
                """, (target_date, target_date))

                # 2. Temporarily rename slot_time for affected rows to avoid UNIQUE(target_date, slot_time) collisions
                for item in retimed_items:
                    conn.execute(
                        "UPDATE schedule SET slot_time = ? WHERE target_date = ? AND post_id = ?",
                        (f"__retime_tmp__{item['post_id']}", target_date, item["post_id"]),
                    )

                # 3. Apply final slot_time and slot_hour to schedule table
                for item in retimed_items:
                    cur = conn.execute(
                        "UPDATE schedule SET slot_time = ?, slot_hour = ? WHERE target_date = ? AND post_id = ?",
                        (item["slot_time"], item["slot_hour"], target_date, item["post_id"]),
                    )
                    db_updated += cur.rowcount

                # 4. Update uploads table (publish_at and slot_time)
                for item in retimed_items:
                    conn.execute(
                        "UPDATE uploads SET publish_at = ?, slot_time = ? WHERE target_date = ? AND post_id = ?",
                        (item["publish_at"], item["slot_time"], target_date, item["post_id"]),
                    )
        finally:
            conn.close()

    # 5. Update schedule.json files
    json_updated = 0
    candidate_paths = [
        actual_data_dir / "edited" / target_date / "schedule.json",
        actual_data_dir / "published" / target_date / "schedule.json",
        actual_data_dir / target_date / "schedule.json",
        actual_data_dir / "schedule.json",
    ]

    retimed_by_post = {item["post_id"]: item for item in retimed_items}

    for json_path in candidate_paths:
        if not json_path.is_file():
            continue

        try:
            # Create a backup copy (.bak) if not already existing
            bak_file = json_path.with_suffix(".json.bak")
            if not bak_file.exists():
                try:
                    shutil.copy2(json_path, bak_file)
                except Exception:
                    pass

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            slots = data.get("slots", [])
            modified = False
            for s in slots:
                pid = s.get("post_id")
                if pid in retimed_by_post:
                    info = retimed_by_post[pid]
                    s["slot_time"] = info["slot_time"]
                    s["slot_hour"] = info["slot_hour"]
                    if "publish_at" in s:
                        s["publish_at"] = info["publish_at"]
                    if "publishAt" in s:
                        s["publishAt"] = info["publish_at"]
                    modified = True

            if modified:
                slots.sort(key=lambda x: x.get("slot_time", ""))
                data["slots"] = slots

                temp_file = json_path.with_name(f"{json_path.name}.tmp.{os.getpid()}")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_file, json_path)
                json_updated += 1
        except Exception:
            pass

    return {"db_updated": db_updated, "json_updated": json_updated}


def rollback_local_retimed_data(
    target_date: str,
    retimed_items: List[Dict[str, Any]],
    db_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> None:
    """
    Roll back local SQLite changes and restore schedule.json from .bak backups
    if YouTube retiming fails.
    """
    if not retimed_items:
        return

    actual_db_path = Path(db_path or DB_PATH).resolve()
    actual_data_dir = Path(data_dir or (WORKSPACE_DIR / "agent" / "data")).resolve()

    if actual_db_path.is_file():
        conn = sqlite3.connect(str(actual_db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                # 1. Temporarily rename to avoid collisions during rollback
                for item in retimed_items:
                    if item.get("old_slot_time"):
                        conn.execute(
                            "UPDATE schedule SET slot_time = ? WHERE target_date = ? AND post_id = ?",
                            (f"__rollback_tmp__{item['post_id']}", target_date, item["post_id"]),
                        )

                # 2. Restore original slot_time and slot_hour to schedule
                for item in retimed_items:
                    if item.get("old_slot_time"):
                        conn.execute(
                            "UPDATE schedule SET slot_time = ?, slot_hour = ? WHERE target_date = ? AND post_id = ?",
                            (item["old_slot_time"], item["old_slot_hour"], target_date, item["post_id"]),
                        )

                # 3. Restore original publish_at and slot_time to uploads
                for item in retimed_items:
                    if item.get("old_publish_at") or item.get("old_slot_time"):
                        conn.execute(
                            "UPDATE uploads SET publish_at = ?, slot_time = ? WHERE target_date = ? AND post_id = ?",
                            (item.get("old_publish_at"), item.get("old_slot_time"), target_date, item["post_id"]),
                        )
        except Exception:
            pass
        finally:
            conn.close()

    # 4. Restore schedule.json from .bak backups if available
    candidate_paths = [
        actual_data_dir / "edited" / target_date / "schedule.json",
        actual_data_dir / "published" / target_date / "schedule.json",
        actual_data_dir / target_date / "schedule.json",
        actual_data_dir / "schedule.json",
    ]
    for json_path in candidate_paths:
        bak_file = json_path.with_suffix(".json.bak")
        if bak_file.is_file():
            try:
                shutil.copy2(bak_file, json_path)
            except Exception:
                pass


def dashboard_today(now: Optional[datetime] = None) -> str:
    """Return today's date in Asia/Kolkata as YYYY-MM-DD."""
    from uploader.dates import get_ist_today
    return get_ist_today(now=now)


def classify_task_result(code: Optional[int]) -> Optional[str]:
    """
    Turn a Windows Task Scheduler LastTaskResult into an error message, or None.

    - 0 = success.
    - 0x0004xxxx = informational (running, not yet run, ready, ...), not an error.
    - Small values (below 0x10000) are the exit code of run_daily.py itself. The task DID start;
      the run summary already shows whether the pipeline failed, so no "failed to start" message.
    - 0xC0000000 and above = the process crashed (NTSTATUS).
    - Other 0x8xxxxxxx values = Windows could not launch the task (for example 0x8007010B,
      "directory name is invalid").
    """
    if code is None:
        return None
    try:
        code = int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None
    if code == 0 or 0x00040000 <= code < 0x00050000 or code < 0x10000:
        return None
    if code >= 0xC0000000:
        return f"Daily task crashed (0x{code:08X})"
    return f"Daily task failed to start (0x{code:08X})"


def get_task_scheduler_info() -> Dict[str, Any]:
    """
    Query Windows Task Scheduler for the daily video bot task via PowerShell Get-ScheduledTaskInfo.
    Returns:
        {
            "next_run_iso": Optional[str],
            "last_task_result": Optional[int],
            "last_task_error": Optional[str],
        }
    """
    if os.name != "nt":
        return {"next_run_iso": None, "last_task_result": None, "last_task_error": None}

    task_name = "100B Daily Video Bot"
    try:
        cfg_path = WORKSPACE_DIR / "agent" / "config.yaml"
        if cfg_path.is_file():
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
                task_name = c.get("daily_run", {}).get("task_name", task_name)
    except Exception:
        pass

    try:
        import subprocess
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Get-ScheduledTaskInfo -TaskName '{task_name}' | ConvertTo-Json"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if proc.returncode != 0:
            return {
                "next_run_iso": None,
                "last_task_result": None,
                "last_task_error": f"Daily task '{task_name}' failed to start or not found",
            }

        data = json.loads(proc.stdout.strip())
        next_run_raw = data.get("NextRunTime")
        next_run_iso = None
        if next_run_raw:
            m = re.search(r"/Date\((\d+)\)/", str(next_run_raw))
            if m:
                ts = int(m.group(1)) / 1000.0
                dt_ist = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
                next_run_iso = dt_ist.isoformat()
            else:
                try:
                    dt = datetime.fromisoformat(str(next_run_raw))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                    next_run_iso = dt.isoformat()
                except Exception:
                    pass

        last_result = data.get("LastTaskResult")
        last_error = classify_task_result(last_result)

        return {
            "next_run_iso": next_run_iso,
            "last_task_result": last_result,
            "last_task_error": last_error,
        }
    except Exception:
        return {
            "next_run_iso": None,
            "last_task_result": None,
            "last_task_error": None,
        }


def get_fallback_next_run_time() -> str:
    """
    Fallback when the Windows task cannot be read:
    Compute next time_ist in Asia/Kolkata from agent/config.yaml (defaults to 07:00).
    """
    time_ist = "07:00"
    try:
        cfg_path = WORKSPACE_DIR / "agent" / "config.yaml"
        if cfg_path.is_file():
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
                time_ist = c.get("daily_run", {}).get("time_ist", "07:00")
    except Exception:
        pass

    try:
        parts = time_ist.strip().split(":")
        th = int(parts[0])
        tm = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        th, tm = 7, 0

    tz_ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    target_dt = now_ist.replace(hour=th, minute=tm, second=0, microsecond=0)
    if now_ist >= target_dt:
        target_dt += timedelta(days=1)
    return target_dt.isoformat()


def get_next_run_time_from_scheduler() -> Optional[str]:
    """Compatibility alias returning next run ISO."""
    info = get_task_scheduler_info()
    return info.get("next_run_iso")


_YOUTUBE_AUTH_CACHE: Dict[str, Any] = {
    "status": None,
    "cached_at": 0.0,
}


def get_cached_youtube_auth() -> Dict[str, Any]:
    """Check YouTube OAuth token validity, cached for 10 minutes to prevent API spam."""
    global _YOUTUBE_AUTH_CACHE
    now = time.time()
    if _YOUTUBE_AUTH_CACHE["status"] is not None and (now - _YOUTUBE_AUTH_CACHE["cached_at"] < 600.0):
        return _YOUTUBE_AUTH_CACHE["status"]

    try:
        from uploader.auth import check_youtube_auth
        res = check_youtube_auth(secrets_dir=WORKSPACE_DIR / "uploader" / "secrets")
    except Exception as e:
        res = {"status": "error", "message": str(e)}

    _YOUTUBE_AUTH_CACHE["status"] = res
    _YOUTUBE_AUTH_CACHE["cached_at"] = now
    return res


def get_pipeline_status() -> Dict[str, Any]:
    """Return last run info from daily_runs, next run time, and task scheduler health."""
    with get_ro_connection() as conn:
        try:
            row = conn.execute("""
                SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1
            """).fetchone()
        except sqlite3.OperationalError:
            row = None

    last_run = None
    if row:
        row_keys = row.keys() if hasattr(row, "keys") else []
        last_run = {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "uploaded": row["uploaded"],
            "scheduled": row["scheduled"],
            "failed": row["failed"],
            "message": row["message"],
            "next_attempt_at": row["next_attempt_at"] if "next_attempt_at" in row_keys else None,
        }

    task_info = get_task_scheduler_info()
    next_run_iso = task_info.get("next_run_iso") or get_fallback_next_run_time()
    last_task_result = task_info.get("last_task_result")
    last_task_error = task_info.get("last_task_error")

    # Reddit Breaker / Request status
    reddit_status = {
        "status": "ok",
        "requests_today": 0,
        "max_requests_per_day": 80,
        "blocked_until": None,
    }
    vdb_path = WORKSPACE_DIR / "scraper" / "data" / "videos.db"
    if vdb_path.is_file():
        try:
            vconn = sqlite3.connect(f"file:{vdb_path.resolve()}?mode=ro", uri=True, timeout=5.0)
            vconn.row_factory = sqlite3.Row
            today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

            req_row = vconn.execute(
                "SELECT COUNT(*) as cnt FROM request_log WHERE day = ? AND host = 'reddit'",
                (today_ist,)
            ).fetchone()
            req_today = int(req_row["cnt"]) if req_row else 0
            reddit_status["requests_today"] = req_today

            b_rows = vconn.execute("SELECT * FROM reddit_breaker").fetchall()
            now_utc = datetime.now(timezone.utc)
            active_blocks = []
            for b in b_rows:
                b_until = b["blocked_until"]
                if b_until:
                    try:
                        b_dt = datetime.fromisoformat(b_until)
                        if b_dt.tzinfo is None:
                            b_dt = b_dt.replace(tzinfo=timezone.utc)
                        if b_dt > now_utc:
                            ist_dt = b_dt.astimezone(ZoneInfo("Asia/Kolkata"))
                            active_blocks.append(f"{b['layer']} blocked until {ist_dt.strftime('%I:%M %p IST').lstrip('0')}")
                    except Exception:
                        pass

            if active_blocks:
                reddit_status["status"] = "blocked"
                reddit_status["blocked_until"] = "; ".join(active_blocks)
            elif req_today >= 70:
                reddit_status["status"] = "throttled"
            vconn.close()
        except Exception:
            pass

    return {
        "last_run": last_run,
        "next_run_time": next_run_iso,
        "last_task_result": last_task_result,
        "last_task_error": last_task_error,
        "channel_timezone": "America/New_York",
        "display_timezone": "Asia/Kolkata",
        "reddit": reddit_status,
        "youtube_auth": get_cached_youtube_auth(),
    }


def is_pipeline_active() -> bool:
    """Check if daily_run.lock exists and its PID is alive."""
    lock_file = WORKSPACE_DIR / "daily_run.lock"
    if not lock_file.is_file():
        return False
    try:
        content = json.loads(lock_file.read_text(encoding="utf-8"))
        pid = int(content.get("pid", 0))
        if pid <= 0:
            return False
        import sys
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process:
                kernel32.CloseHandle(process)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def get_agent_days(num_days: int = 7) -> List[Dict[str, Any]]:
    """
    Return summary statistics for the last `num_days` days for the day picker.
    Ends strictly at dashboard_today() (today in Asia/Kolkata).
    Starts with today selected.
    """
    from datetime import timedelta
    today_str = dashboard_today()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")

    with get_ro_connection() as conn:
        dates_set = set()
        # Query distinct dates from DB, but only up to today (ignore future test dates)
        try:
            for r in conn.execute(
                "SELECT DISTINCT target_date FROM run_stats WHERE target_date <= ? ORDER BY target_date DESC LIMIT ?",
                (today_str, num_days)
            ):
                if r[0]: dates_set.add(r[0])
        except sqlite3.OperationalError:
            pass

        try:
            for r in conn.execute(
                "SELECT DISTINCT target_date FROM schedule WHERE target_date <= ? ORDER BY target_date DESC LIMIT ?",
                (today_str, num_days)
            ):
                if r[0]: dates_set.add(r[0])
        except sqlite3.OperationalError:
            pass

        try:
            for r in conn.execute(
                "SELECT DISTINCT target_date FROM agent_decisions WHERE target_date <= ? ORDER BY target_date DESC LIMIT ?",
                (today_str, num_days)
            ):
                if r[0]: dates_set.add(r[0])
        except sqlite3.OperationalError:
            pass

        # Ensure contiguous num_days strip ending at today
        for offset in range(-(num_days - 1), 1):
            dates_set.add((today_dt + timedelta(days=offset)).strftime("%Y-%m-%d"))

        # Take the last num_days ending at today
        valid_dates = [d for d in dates_set if d <= today_str]
        sorted_dates = sorted(valid_dates, reverse=True)[:num_days]
        sorted_dates.sort()  # Ascending order for the 7-day strip (older -> newer, ending at today)

        results = []
        is_running = is_pipeline_active()

        for d in sorted_dates:
            day_data = _get_day_stats_internal(conn, d)
            day_data["date"] = d
            day_data["is_today"] = (d == today_str)
            day_data["is_running"] = (d == today_str and is_running)

            if d == today_str and not is_running and day_data.get("found", 0) == 0 and day_data.get("approved", 0) == 0:
                day_data["pending_run"] = True
            else:
                day_data["pending_run"] = False

            results.append(day_data)

    return results


def _get_day_stats_internal(conn: sqlite3.Connection, target_date: str) -> Dict[str, Any]:
    """Helper to get stats from run_stats (aggregating all runs for target_date) or fallback to agent_decisions."""
    # 1. Try run_stats rows for this date (aggregate all passes/runs for the date)
    try:
        rows = conn.execute("""
            SELECT * FROM run_stats
            WHERE target_date = ?
            ORDER BY id ASC
        """, (target_date,)).fetchall()
        if rows:
            total_found = sum(r["found"] for r in rows)
            total_approved = sum(r["approved"] for r in rows)
            total_selected = sum(r["selected"] for r in rows)
            total_downloaded = sum(r["downloaded"] for r in rows)
            total_judged = sum(r["judged"] for r in rows)
            total_rejected = sum(r["rejected_judge"] for r in rows)
            total_dropped_prescreen = sum(r["dropped_prescreen"] for r in rows)
            total_dropped_duration = sum(r["dropped_duration"] for r in rows)
            total_dropped_filters = sum(r["dropped_filters"] for r in rows)
            total_borderline = sum(r["borderline"] for r in rows)
            total_reserve_added = sum(r["reserve_added"] for r in rows)

            merged_reasons: Dict[str, Any] = {}
            merged_sources: Dict[str, Any] = {}
            for r in rows:
                for k, merged in (("reject_reasons_json", merged_reasons), ("source_stats_json", merged_sources)):
                    val = r[k]
                    data = {}
                    if isinstance(val, str):
                        try:
                            data = json.loads(val)
                        except Exception:
                            data = {}
                    elif isinstance(val, dict):
                        data = val
                    for subk, subv in data.items():
                        if isinstance(subv, (int, float)):
                            merged[subk] = merged.get(subk, 0) + subv
                        elif isinstance(subv, dict):
                            merged.setdefault(subk, {})
                            for ssubk, ssubv in subv.items():
                                if isinstance(ssubv, (int, float)):
                                    merged[subk][ssubk] = merged[subk].get(ssubk, 0) + ssubv

            # Reconcile with actual schedule table count if present
            try:
                sched_row = conn.execute("SELECT COUNT(*) as cnt FROM schedule WHERE target_date = ? AND status != 'empty'", (target_date,)).fetchone()
                actual_scheduled = sched_row["cnt"] if sched_row else total_selected
                if actual_scheduled > total_approved:
                    total_approved = actual_scheduled
                if actual_scheduled > total_found:
                    total_found = actual_scheduled
            except Exception:
                actual_scheduled = total_selected

            return {
                "id": rows[-1]["id"],
                "target_date": target_date,
                "found": total_found,
                "dropped_prescreen": total_dropped_prescreen,
                "dropped_duration": total_dropped_duration,
                "downloaded": total_downloaded,
                "dropped_filters": total_dropped_filters,
                "judged": total_judged,
                "rejected_judge": total_rejected,
                "borderline": total_borderline,
                "approved": total_approved,
                "selected": actual_scheduled,
                "reserve_added": total_reserve_added,
                "reject_reasons_json": merged_reasons,
                "source_stats_json": merged_sources,
                "created_at": rows[-1]["created_at"],
                "estimated": False,
            }
    except sqlite3.OperationalError:
        pass

    # 2. Fallback to deduplicated agent_decisions
    # agent_decisions is an event log: use latest row per post_id and target_date
    try:
        cur = conn.execute("""
            WITH latest_decisions AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY post_id, target_date ORDER BY id DESC) as rn
                FROM agent_decisions
                WHERE target_date = ?
            )
            SELECT status, decision_reason, hybrid_score
            FROM latest_decisions
            WHERE rn = 1
        """, (target_date,))
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []

    # Get scheduled count
    try:
        sched_row = conn.execute("SELECT COUNT(*) as cnt FROM schedule WHERE target_date = ?", (target_date,)).fetchone()
        selected_count = sched_row["cnt"] if sched_row else 0
    except sqlite3.OperationalError:
        selected_count = 0

    # Get available reserve count
    try:
        res_row = conn.execute("SELECT COUNT(*) as cnt FROM reserve_pool WHERE status = 'available'").fetchone()
        reserve_count = res_row["cnt"] if res_row else 0
    except sqlite3.OperationalError:
        reserve_count = 0

    if not rows:
        return {
            "target_date": target_date,
            "found": 0,
            "dropped_prescreen": 0,
            "dropped_duration": 0,
            "downloaded": 0,
            "dropped_filters": 0,
            "judged": 0,
            "rejected_judge": 0,
            "borderline": 0,
            "approved": selected_count,
            "selected": selected_count,
            "reserve_added": 0,
            "reject_reasons_json": {
                "not_funny": 0, "non_english": 0, "watermark": 0, "duration": 0, "tv_film": 0,
                "rights_agency": 0, "rights_music": 0, "rights_visual_match": 0, "rights_history": 0, "rights_social_watermark": 0, "other": 0
            },
            "source_stats_json": {},
            "estimated": True,
        }

    accepted_count = sum(1 for r in rows if r["status"] in ("auto_approved", "approved"))
    rejected_count = sum(1 for r in rows if r["status"] in ("rejected", "auto_rejected"))
    borderline_count = sum(1 for r in rows if r["status"] == "borderline")
    found_count = len(rows)

    reasons = {
        "not_funny": 0, "non_english": 0, "watermark": 0, "duration": 0, "tv_film": 0,
        "rights_agency": 0, "rights_music": 0, "rights_visual_match": 0, "rights_history": 0, "rights_social_watermark": 0, "other": 0
    }
    for r in rows:
        if r["status"] in ("rejected", "auto_rejected"):
            txt = (r["decision_reason"] or "").lower()
            if "rights_agency" in txt or "agency" in txt or "viralhog" in txt or "collab" in txt:
                reasons["rights_agency"] += 1
            elif "rights_music" in txt or "music" in txt:
                reasons["rights_music"] += 1
            elif "rights_visual" in txt or "visual_match" in txt:
                reasons["rights_visual_match"] += 1
            elif "rights_history" in txt or "duplicate" in txt:
                reasons["rights_history"] += 1
            elif "rights_social" in txt or "social_watermark" in txt:
                reasons["rights_social_watermark"] += 1
            elif any(w in txt for w in ("not funny", "score", "threshold", "unfunny")):
                reasons["not_funny"] += 1
            elif any(w in txt for w in ("english", "language", "speech")):
                reasons["non_english"] += 1
            elif any(w in txt for w in ("watermark", "logo", "channel")):
                reasons["watermark"] += 1
            elif any(w in txt for w in ("duration", "seconds", "too long", "too short")):
                reasons["duration"] += 1
            elif any(w in txt for w in ("tv", "film", "movie")):
                reasons["tv_film"] += 1
            else:
                reasons["other"] += 1

    return {
        "target_date": target_date,
        "found": found_count,
        "dropped_prescreen": 0,
        "dropped_duration": reasons["duration"],
        "downloaded": found_count,
        "dropped_filters": reasons["non_english"] + reasons["duration"],
        "judged": found_count,
        "rejected_judge": rejected_count,
        "borderline": borderline_count,
        "approved": accepted_count,
        "selected": selected_count or accepted_count,
        "reserve_added": max(0, accepted_count - selected_count),
        "reject_reasons_json": reasons,
        "source_stats_json": {},
        "estimated": True,
    }


def get_agent_day_detail(target_date: str) -> Dict[str, Any]:
    """
    Detailed data for Agent view: funnel ribbon, totals, lineup (for next day per design),
    reserve pool rows, rejection reasons, and source yield.
    Under a selected day, shows the next day's curated clips (tomorrow's lineup).
    """
    from datetime import timedelta
    try:
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        next_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        next_date = target_date

    with get_ro_connection() as conn:
        stats = _get_day_stats_internal(conn, target_date)

        # Lineup: Under a selected day, show the next day's curated clips (tomorrow's lineup)
        lineup_rows = []
        try:
            cur = conn.execute("""
                WITH latest_dec AS (
                    SELECT post_id, hybrid_score, ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY id DESC) as rn
                    FROM agent_decisions
                )
                SELECT s.slot_time, s.slot_hour, s.post_id, s.title, s.uploader, s.source_url, s.video_path,
                       s.rights_status, s.rights_risk,
                       COALESCE(d.hybrid_score, r.score) as hybrid_score
                FROM schedule s
                LEFT JOIN latest_dec d ON s.post_id = d.post_id AND d.rn = 1
                LEFT JOIN reserve_pool r ON s.post_id = r.post_id
                WHERE s.target_date = ?
                ORDER BY s.slot_time ASC
            """, (next_date,))
            lineup_rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            pass

        has_next_date_lineup = bool(lineup_rows)

        # Fallback if no rows found for next_date: check target_date itself
        if not lineup_rows:
            try:
                cur = conn.execute("""
                    WITH latest_dec AS (
                        SELECT post_id, hybrid_score, ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY id DESC) as rn
                        FROM agent_decisions
                    )
                    SELECT s.slot_time, s.slot_hour, s.post_id, s.title, s.uploader, s.source_url, s.video_path,
                           s.rights_status, s.rights_risk,
                           COALESCE(d.hybrid_score, r.score) as hybrid_score
                    FROM schedule s
                    LEFT JOIN latest_dec d ON s.post_id = d.post_id AND d.rn = 1
                    LEFT JOIN reserve_pool r ON s.post_id = r.post_id
                    WHERE s.target_date = ?
                    ORDER BY s.slot_time ASC
                """, (target_date,))
                lineup_rows = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                lineup_rows = []

        lineup_target_date = next_date if has_next_date_lineup else target_date

        # Format time_display in Asia/Kolkata (IST)
        for idx, r in enumerate(lineup_rows):
            slot_iso = r.get("slot_time") or ""
            time_disp = "10:30 PM IST"
            try:
                dt_iso = datetime.fromisoformat(slot_iso)
                if dt_iso.tzinfo is None:
                    dt_iso = dt_iso.replace(tzinfo=ZoneInfo("America/New_York"))
                dt_ist = dt_iso.astimezone(ZoneInfo("Asia/Kolkata"))
                if dt_ist.minute == 0:
                    time_disp = f"{dt_ist.strftime('%I').lstrip('0')} {dt_ist.strftime('%p')} IST"
                else:
                    time_disp = f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
            except Exception:
                h = r.get("slot_hour", 12 + idx)
                try:
                    d_obj = datetime.strptime(lineup_target_date, "%Y-%m-%d")
                    dt_et = datetime(d_obj.year, d_obj.month, d_obj.day, h, 0, 0, tzinfo=ZoneInfo("America/New_York"))
                    dt_ist = dt_et.astimezone(ZoneInfo("Asia/Kolkata"))
                    if dt_ist.minute == 0:
                        time_disp = f"{dt_ist.strftime('%I').lstrip('0')} {dt_ist.strftime('%p')} IST"
                    else:
                        time_disp = f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
                except Exception:
                    period = "PM" if h >= 12 else "AM"
                    h12 = h % 12 or 12
                    time_disp = f"{h12} {period} IST"
            r["time_display"] = time_disp
            r["thumb_url"] = f"/api/thumbs/{r['post_id']}.jpg"

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        today_str = now_ny.strftime("%Y-%m-%d")
        is_today = (target_date == today_str)

        # Reserve clips: today shows currently available pool; past days show clips added on that day
        if is_today:
            try:
                cur = conn.execute("""
                    SELECT post_id, title, uploader, score, duration, status
                    FROM reserve_pool
                    WHERE status = 'available'
                    ORDER BY score DESC
                    LIMIT 12
                """)
                reserve_rows = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                reserve_rows = []
            stored_reserve = stats.get("reserve_added") if stats.get("reserve_added") is not None else len(reserve_rows)
        else:
            try:
                cur = conn.execute("""
                    SELECT post_id, title, uploader, score, duration, status
                    FROM reserve_pool
                    WHERE DATE(added_at) = ? OR added_at LIKE ? || '%'
                    ORDER BY score DESC
                    LIMIT 12
                """, (target_date, target_date))
                reserve_rows = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                reserve_rows = []
            stored_reserve = stats.get("reserve_added", len(reserve_rows))

        # Sources summary: for past days, use that day's stats only (never today's running total)
        source_stats = stats.get("source_stats_json") or {}
        if not source_stats:
            derived_sources: Dict[str, Dict[str, int]] = {}
            try:
                cur = conn.execute("SELECT source_url FROM schedule WHERE target_date = ?", (target_date,))
                for (s_url,) in cur.fetchall():
                    if s_url and "/r/" in s_url:
                        parts = s_url.split("/r/")[1].split("/")
                        if parts and parts[0]:
                            src = f"r/{parts[0]}"
                            derived_sources.setdefault(src, {"judged": 0, "accepted": 0})
                            derived_sources[src]["accepted"] += 1
                            derived_sources[src]["judged"] += 1

                cur = conn.execute("SELECT post_url FROM reserve_pool WHERE DATE(added_at) = ? OR added_at LIKE ? || '%'", (target_date, target_date))
                for (p_url,) in cur.fetchall():
                    if p_url and "/r/" in p_url:
                        parts = p_url.split("/r/")[1].split("/")
                        if parts and parts[0]:
                            src = f"r/{parts[0]}"
                            derived_sources.setdefault(src, {"judged": 0, "accepted": 0})
                            derived_sources[src]["accepted"] += 1
                            derived_sources[src]["judged"] += 1
            except Exception:
                pass

            if derived_sources:
                source_stats = derived_sources
            elif is_today:
                try:
                    cur = conn.execute("SELECT name, downloaded_count, approved_count FROM sources WHERE status != 'exhausted' ORDER BY yield_rate DESC LIMIT 6")
                    for r in cur.fetchall():
                        source_stats[f"r/{r['name']}"] = {
                            "judged": r["downloaded_count"],
                            "accepted": r["approved_count"],
                        }
                except sqlite3.OperationalError:
                    pass
            else:
                source_stats = {}

        # Funnel counts for ribbon
        # Proportions: dropped_before_judging, rejected_by_judge, stored_in_reserve, selected_for_tomorrow
        dropped_before = stats.get("dropped_prescreen", 0) + stats.get("dropped_filters", 0)
        rejected_judge = stats.get("rejected_judge", 0)
        selected_tomorrow = len(lineup_rows) if lineup_rows else stats.get("selected", 0)

        # Fallback if totals are 0
        total_found = stats.get("found", 0)
        total_accepted = stats.get("approved", 0)
        total_rejected = dropped_before + rejected_judge

        funnel = {
            "dropped_before_judging": dropped_before,
            "rejected_by_judge": rejected_judge,
            "stored_in_reserve": stored_reserve,
            "selected_for_tomorrow": selected_tomorrow,
        }

        # Add thumbnail URLs to lineup rows
        for item in lineup_rows:
            item["thumb_url"] = f"/api/thumbs/{item['post_id']}.jpg"

        return {
            "target_date": target_date,
            "lineup_target_date": lineup_target_date,
            "estimated": stats.get("estimated", False),
            "totals": {
                "found": total_found,
                "rejected": total_rejected,
                "accepted": total_accepted,
            },
            "funnel": funnel,
            "lineup": lineup_rows,
            "reserve": reserve_rows,
            "reasons": stats.get("reject_reasons_json", {}),
            "sources": source_stats,
        }
