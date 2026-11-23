#!/usr/bin/env python3
"""
==============================================================================
               100B VIDEO BOT — DAILY MASTER ORCHESTRATOR
==============================================================================

Automates the complete daily production and upload cycle:
  1. Locks execution to prevent overlapping runs (with PID stale lock detection)
  2. Runs curation: agent/curate.py --date tomorrow --no-review --borderline-policy approve_top
  3. Runs upload: upload.py upload --date tomorrow
  4. Runs post-upload verification: upload.py verify --date tomorrow
  5. Runs safe cleanup: moves uploaded files to published/, purges media > 14 days old
  6. Dispatches consolidated Telegram / Windows notification summary
"""

import sys
import os
import re
import json
import time
import argparse
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from uploader.alerts import send_telegram_alert
from uploader.cleanup import archive_verified_slot, purge_old_media
from uploader.db import UploadDatabase, atomic_write_json


BASE_DIR = Path(__file__).resolve().parent
LOCK_FILE = BASE_DIR / "daily_run.lock"


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process:
                kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class DailyLockManager:
    """Acquires a process lock with automatic stale PID detection and cleanup."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.acquired = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                content = json.loads(self.lock_path.read_text(encoding="utf-8"))
                existing_pid = int(content.get("pid", 0))
                if is_pid_alive(existing_pid):
                    logger.error(f"Another daily run is already active (PID {existing_pid}). Halting.")
                    return False
                else:
                    logger.warning(f"Detected stale lock file from dead PID {existing_pid}. Removing stale lock.")
                    self.lock_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Corrupt lock file detected ({e}); replacing.")
                self.lock_path.unlink(missing_ok=True)

        # Write current PID
        lock_data = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
        }
        try:
            self.lock_path.write_text(json.dumps(lock_data), encoding="utf-8")
            self.acquired = True
            logger.info(f"Acquired master daily lock (PID: {os.getpid()})")
            return True
        except Exception as e:
            logger.error(f"Could not create lock file: {e}")
            return False

    def release(self):
        if self.acquired and self.lock_path.exists():
            try:
                self.lock_path.unlink(missing_ok=True)
                logger.info("Released master daily lock.")
            except Exception as e:
                logger.warning(f"Error removing lock file: {e}")
        self.acquired = False

    def __enter__(self):
        if not self.acquire():
            sys.exit(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def run_step(name: str, cmd: list, timeout_s: int = 3600) -> bool:
    """Execute a single pipeline step in a subprocess with isolated timeout."""
    logger.info(f"\n{'='*70}\n[STEP] Starting: {name}\nCommand: {' '.join(cmd)}\n{'='*70}")
    start = time.time()
    try:
        proc = subprocess.run(cmd, timeout=timeout_s, cwd=str(BASE_DIR))
        elapsed = round(time.time() - start, 1)
        if proc.returncode == 0:
            logger.success(f"[STEP SUCCESS] {name} completed in {elapsed}s.")
            return True
        else:
            logger.error(f"[STEP FAILED] {name} exited with code {proc.returncode} in {elapsed}s.")
            return False
    except subprocess.TimeoutExpired:
        logger.critical(f"[STEP TIMEOUT] {name} exceeded {timeout_s}s timeout!")
        return False
    except Exception as e:
        logger.error(f"[STEP EXCEPTION] {name} failed: {e}")
        return False


def batch_already_curated(schedule_path: Path, target_count: int) -> bool:
    """
    True when schedule.json already holds `target_count` ready slots (queued/scheduled/uploaded)
    whose edited video files exist on disk.

    A retry trigger (for example after the YouTube upload failed) must then go straight to the
    upload step. Running curation again would replace clips and burn reserve-pool clips for nothing.
    """
    try:
        with open(schedule_path, "r", encoding="utf-8") as f:
            slots = json.load(f).get("slots", [])
    except Exception:
        return False
    ready = 0
    for slot in slots:
        video_path = slot.get("video_path")
        if (
            slot.get("status") in ("queued", "scheduled", "uploaded")
            and slot.get("post_id")
            and video_path
            and Path(video_path).is_file()
        ):
            ready += 1
    return ready >= target_count


def main():
    parser = argparse.ArgumentParser(description="100B Automated Video Bot — Daily Orchestration")
    parser.add_argument("--date", default="tomorrow", help="Target date ('tomorrow' or 'YYYY-MM-DD')")
    parser.add_argument("--dry-run", action="store_true", help="Simulate pipeline without real uploads or paid APIs")
    parser.add_argument("--confirm-public", action="store_true", help="Allow public / scheduled releases")
    parser.add_argument("--skip-curation", action="store_true", help="Skip curation step and go straight to upload")
    parser.add_argument("--skip-upload", action="store_true", help="Skip upload step (curation only)")
    parser.add_argument("--timeout", type=int, default=7200, help="Global pipeline timeout in seconds (default: 7200)")
    parser.add_argument("--db-path", default=None, help="Explicit path to SQLite database (defaults to AGENT_DB_PATH or agent/data/agent.db)")

    args = parser.parse_args()

    # Acquire Master Process Lock
    with DailyLockManager(LOCK_FILE):
        from uploader.dates import resolve_date_arg, get_ist_today
        from upload import load_uploader_config
        config = load_uploader_config()
        tz_name = config.get("timezone", "America/New_York")
        target_date_str = resolve_date_arg(args.date, tz_name)
        target_count = int(config.get("upload", {}).get("max_uploads_per_day", 6))
        schedule_path = BASE_DIR / "agent" / "data" / "edited" / target_date_str / "schedule.json"

        # Initialize UploadDatabase and log daily run start (Phase 5 instrumentation)
        db_path = Path(args.db_path) if args.db_path else Path(os.environ.get("AGENT_DB_PATH", BASE_DIR / "agent" / "data" / "agent.db"))
        db = UploadDatabase(db_path)

        # Idempotency check: If all slots are already uploaded or scheduled for target_date_str, exit 0
        existing_uploaded = db.get_daily_upload_count(target_date_str)
        if existing_uploaded >= target_count:
            logger.info(f"Target date {target_date_str} already complete ({existing_uploaded}/{target_count} uploads in DB). Exiting cleanly.")
            sys.exit(0)

        if schedule_path.is_file():
            try:
                with open(schedule_path, "r", encoding="utf-8") as f:
                    sc_data = json.load(f)
                sc_slots = sc_data.get("slots", [])
                if len(sc_slots) >= target_count and all(s.get("status") in ("uploaded", "scheduled") for s in sc_slots):
                    logger.info(f"Target date {target_date_str} already complete ({len(sc_slots)}/{target_count} slots in schedule.json). Exiting cleanly.")
                    sys.exit(0)
            except Exception:
                pass

        run_id = db.record_daily_run_start()

        # Step 0: Early YouTube OAuth Health Guard (Phase 5)
        if not args.dry_run:
            from uploader.auth import check_youtube_auth
            auth_info = check_youtube_auth(secrets_dir=BASE_DIR / "uploader" / "secrets")
            if auth_info["status"] in ("expired", "missing"):
                auth_msg = (
                    "YouTube login expired. Run: python upload.py auth and "
                    "python upload.py auth --manage on the machine to re-authorize."
                )
                logger.error(f"[Auth Guard] {auth_msg} Detail: {auth_info.get('message')}")
                send_telegram_alert("AUTH_REQUIRED", auth_msg, {"detail": auth_info.get("message")})
                db.record_daily_run_finish(
                    run_id=run_id,
                    status="auth_required",
                    uploaded=0,
                    scheduled=0,
                    failed=0,
                    message=f"Daily run halted for date {target_date_str}: {auth_msg}",
                )
                sys.exit(3)
            elif auth_info["status"] == "error":
                logger.warning(
                    f"[Auth Guard] YouTube auth check returned transient error: {auth_info.get('message')}. "
                    "Proceeding with run."
                )

        logger.info(f"=== Starting Daily Bot Run for Date: {target_date_str} (Target: {target_count} clips | Dry Run: {args.dry_run}) ===")

        # Step 1: Curation
        curation_ok = True
        curated_count = 0
        reuse_existing_batch = (
            not args.skip_curation and schedule_path.is_file() and batch_already_curated(schedule_path, target_count)
        )
        if reuse_existing_batch:
            logger.info(
                f"Batch for {target_date_str} is already curated ({target_count}/{target_count} slots ready on disk). "
                "Skipping curation and going straight to upload."
            )
        if not args.skip_curation and not reuse_existing_batch:
            curate_cmd = [
                sys.executable,
                str(BASE_DIR / "agent" / "curate.py"),
                "--date", target_date_str,
                "--no-review",
                "--borderline-policy", "approve_top",
            ]
            if args.dry_run:
                curate_cmd.append("--dry-run")

            curation_ok = run_step("Video Curation (LangGraph)", curate_cmd, timeout_s=3600)

        # Inspect curation outcome from schedule.json
        if schedule_path.is_file():
            try:
                with open(schedule_path, "r", encoding="utf-8") as f:
                    sched = json.load(f)
                curated_count = len([
                    s for s in sched.get("slots", [])
                    if s.get("status") not in ("empty", "failed", "held")
                    and s.get("post_id")
                    and (not s.get("video_path") or args.dry_run or Path(s.get("video_path")).is_file())
                ])
            except Exception as e:
                logger.warning(f"Could not read schedule.json at {schedule_path}: {e}")
                curated_count = 0
        else:
            curated_count = 0

        # If curation yielded fewer than target_count clips, attempt backfill from reserve pool first
        if not args.skip_curation and curated_count < target_count:
            logger.info(f"Curation yielded {curated_count}/{target_count} slots. Attempting backfill from reserve pool...")
            fill_cmd = [
                sys.executable,
                str(BASE_DIR / "agent" / "curate.py"),
                "--fill-slots", target_date_str,
            ]
            if args.dry_run:
                fill_cmd.append("--dry-run")
            run_step("Reserve Pool Backfill", fill_cmd, timeout_s=300)

            if schedule_path.is_file():
                try:
                    with open(schedule_path, "r", encoding="utf-8") as f:
                        sched = json.load(f)
                    curated_count = len([
                        s for s in sched.get("slots", [])
                        if s.get("status") not in ("empty", "failed", "held")
                        and s.get("post_id")
                        and (not s.get("video_path") or args.dry_run or Path(s.get("video_path")).is_file())
                    ])
                except Exception:
                    pass

        logger.info(f"Curation summary for {target_date_str}: {curated_count}/{target_count} slots curated.")

        # Check if scraper was blocked
        is_scraper_blocked = False
        blocked_until_str = None
        vdb_path = BASE_DIR / "scraper" / "data" / "videos.db"
        if vdb_path.is_file():
            try:
                agent_dir = str(BASE_DIR / "agent")
                if agent_dir not in sys.path:
                    sys.path.insert(0, agent_dir)
                from tools.scraper_tool import ScraperTool
                st = ScraperTool(scraper_dir=BASE_DIR / "scraper")
                r_status = st.get_reddit_status()
                if r_status.get("all_layers_open") or r_status.get("any_layer_open"):
                    is_scraper_blocked = True
                    breaker = r_status.get("breaker", {})
                    open_msgs = [f"{l} open ({breaker[l].get('reason')})" for l in breaker if breaker[l].get("is_open")]
                    blocked_until_str = "; ".join(open_msgs) if open_msgs else "circuit breaker active"
            except Exception as e:
                logger.debug(f"Could not check breaker via ScraperTool: {e}")

        scrape_res_path = BASE_DIR / "scraper" / "data" / "candidates" / f"{target_date_str}_scrape_result.json"
        if scrape_res_path.is_file():
            try:
                with open(scrape_res_path, "r", encoding="utf-8") as srf:
                    sr_data = json.load(srf)
                if sr_data.get("status") == "blocked":
                    is_scraper_blocked = True
                    blocked_until_str = sr_data.get("blocked_until") or blocked_until_str
            except Exception:
                pass

        if not args.skip_curation and curated_count < target_count and is_scraper_blocked:
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
            current_ist_hour = now_ist.hour

            if current_ist_hour < 19:
                next_trigger = "20:00 IST"
                for th in [10, 13, 16, 19]:
                    if th > current_ist_hour:
                        next_trigger = f"{th:02d}:00 IST"
                        break

                time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*IST)", str(blocked_until_str))
                blocked_time_str = time_match.group(1) if time_match else (blocked_until_str or "circuit breaker active")
                msg = (
                    f"Daily run WAITING for date {target_date_str}: "
                    f"Scraper blocked until {blocked_time_str}, used {curated_count} reserve clips. "
                    f"Will retry at {next_trigger}."
                )
                logger.warning(msg)
                db.record_daily_run_finish(
                    run_id=run_id,
                    status="waiting",
                    uploaded=0,
                    scheduled=curated_count,
                    failed=0,
                    message=msg,
                    next_attempt_at=next_trigger,
                )
                send_telegram_alert("DAILY_RUN_WAITING", msg, {
                    "target_date": target_date_str,
                    "curated_count": curated_count,
                    "target_count": target_count,
                    "blocked_until": blocked_until_str,
                    "next_attempt_at": next_trigger,
                })
                sys.exit(0)

        # Fail fast if curation yielded 0 clips or curation subprocess crashed
        if not args.skip_curation and (not curation_ok or curated_count == 0):
            msg = (
                f"Daily pipeline FAILED for date {target_date_str}: "
                f"Curation produced {curated_count}/{target_count} approved clips. "
                "No schedule generated; upload aborted."
            )
            logger.error(msg)
            db.record_daily_run_finish(
                run_id=run_id,
                status="failed",
                uploaded=0,
                scheduled=0,
                failed=target_count,
                message=msg,
            )
            send_telegram_alert("DAILY_RUN_FAILED", msg, {
                "target_date": target_date_str,
                "curated_count": curated_count,
                "target_count": target_count,
                "curation_ok": curation_ok,
            })
            sys.exit(1)

        # Step 2: Upload
        upload_ok = True
        if not args.skip_upload:
            upload_cmd = [
                sys.executable,
                str(BASE_DIR / "upload.py"),
                "upload",
                "--date", target_date_str,
            ]
            if args.dry_run:
                upload_cmd.append("--dry-run")
            if args.confirm_public:
                upload_cmd.append("--confirm-public")

            upload_ok = run_step("YouTube Upload Batch", upload_cmd, timeout_s=1800)

            # Step 3: Verification (runs even if upload step had errors to check partial uploads)
            if not args.dry_run:
                verify_cmd = [
                    sys.executable,
                    str(BASE_DIR / "upload.py"),
                    "verify",
                    "--date", target_date_str,
                ]
                run_step("YouTube Post-Upload Verification", verify_cmd, timeout_s=300)

            # Step 3.5: Claims Sync (Gmail & Live Video Probe - runs even after upload failure)
            claims_sync_cmd = [sys.executable, str(BASE_DIR / "claims_sync.py"), "sync"]
            if args.dry_run:
                claims_sync_cmd.append("--dry-run")
            run_step("Claims & Content ID Sync", claims_sync_cmd, timeout_s=600)

            # Step 3.6: Failed-Slot Refill & Re-Upload
            if schedule_path.is_file():
                try:
                    with open(schedule_path, "r", encoding="utf-8") as f:
                        curr_sched = json.load(f)
                    failed_slots = [s for s in curr_sched.get("slots", []) if s.get("status") in ("failed", "error")]
                    if failed_slots:
                        logger.warning(f"Detected {len(failed_slots)} failed upload slot(s). Attempting slot refill...")
                        with db._get_connection() as conn:
                            for fs in failed_slots:
                                conn.execute("""
                                    UPDATE schedule
                                    SET status = 'empty', post_id = NULL, video_path = NULL
                                    WHERE target_date = ? AND slot_time = ?
                                """, (target_date_str, fs.get("slot_time")))
                            conn.commit()

                        for s in curr_sched.get("slots", []):
                            if s.get("status") in ("failed", "error"):
                                s["status"] = "empty"
                                s["post_id"] = None
                                s["video_path"] = None
                        atomic_write_json(schedule_path, curr_sched)

                        # Run fill-slots
                        refill_cmd = [
                            sys.executable,
                            str(BASE_DIR / "agent" / "curate.py"),
                            "--fill-slots", target_date_str,
                        ]
                        if args.dry_run:
                            refill_cmd.append("--dry-run")
                        run_step("Failed Slots Refill", refill_cmd, timeout_s=600)

                        # Re-read schedule.json and check if publication time is > 15m away
                        with open(schedule_path, "r", encoding="utf-8") as f:
                            refilled_sched = json.load(f)

                        now_utc = datetime.now(timezone.utc)
                        can_reupload = False
                        for s in refilled_sched.get("slots", []):
                            if s.get("status") == "scheduled":
                                st_iso = s.get("slot_time")
                                if st_iso:
                                    try:
                                        st_dt = datetime.fromisoformat(st_iso)
                                        if st_dt.tzinfo is None:
                                            st_dt = st_dt.replace(tzinfo=timezone.utc)
                                        if (st_dt - now_utc).total_seconds() > 900:
                                            can_reupload = True
                                            break
                                    except Exception:
                                        pass

                        if can_reupload:
                            logger.info("First refilled slot is > 15m away. Re-running upload step...")
                            reupload_cmd = [
                                sys.executable,
                                str(BASE_DIR / "upload.py"),
                                "upload",
                                "--date", target_date_str,
                            ]
                            if args.dry_run:
                                reupload_cmd.append("--dry-run")
                            if args.confirm_public:
                                reupload_cmd.append("--confirm-public")
                            run_step("YouTube Re-Upload Batch", reupload_cmd, timeout_s=1800)
                except Exception as e:
                    logger.warning(f"Error handling failed slot refill: {e}")

        # Step 4: Cleanup & Archival
        logger.info("\n--- [STEP] Media Archival & Retention Purge ---")
        try:
            if schedule_path.is_file():
                with open(schedule_path, "r", encoding="utf-8") as f:
                    sched = json.load(f)
                for slot in sched.get("slots", []):
                    if slot.get("status") in ("uploaded", "scheduled"):
                        v_path = slot.get("video_path")
                        if v_path and Path(v_path).is_file():
                            new_path = archive_verified_slot(Path(v_path), target_date_str, base_dir=BASE_DIR)
                            if new_path:
                                slot["video_path"] = str(new_path)
                atomic_write_json(schedule_path, sched)

            # Purge media older than 14 days
            purge_old_media(retention_days=14, base_dir=BASE_DIR)
        except Exception as e:
            logger.warning(f"Archival/cleanup step encountered warning: {e}")

        # Step 5: Evaluate Final Outcome (Success, Partial, or Failed)
        # Tally slots status
        uploaded_slots = 0
        scheduled_slots = 0
        failed_slots = 0
        if schedule_path.is_file():
            try:
                with open(schedule_path, "r", encoding="utf-8") as f:
                    final_sched = json.load(f)
                for slot in final_sched.get("slots", []):
                    st = slot.get("status")
                    if st == "uploaded":
                        uploaded_slots += 1
                    elif st == "scheduled":
                        scheduled_slots += 1
                    elif st in ("failed", "error"):
                        failed_slots += 1
            except Exception:
                pass

        # Condition A: Upload step failed
        if not args.skip_upload and not upload_ok:
            msg = f"Daily pipeline FAILED for date {target_date_str}: Upload step exited with errors."
            logger.error(msg)
            db.record_daily_run_finish(
                run_id=run_id,
                status="failed",
                uploaded=uploaded_slots,
                scheduled=scheduled_slots,
                failed=failed_slots or 1,
                message=msg,
            )
            send_telegram_alert("DAILY_RUN_FAILED", msg, {
                "target_date": target_date_str,
                "curated_count": curated_count,
                "target_count": target_count,
                "upload_ok": False,
            })
            sys.exit(1)

        # Condition B: Curation gave fewer than target_count (e.g. 6) clips -> "partial"
        if not args.skip_curation and curated_count < target_count:
            if is_scraper_blocked:
                time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*IST)", str(blocked_until_str))
                blocked_time_str = time_match.group(1) if time_match else (blocked_until_str or "cooldown")
                msg = (
                    f"Daily pipeline finished as PARTIAL for date {target_date_str}: "
                    f"Scraper blocked until {blocked_time_str}, used {curated_count} reserve clips. "
                    f"Upload completed for the available clips."
                )
            else:
                msg = (
                    f"Daily pipeline finished as PARTIAL for date {target_date_str}: "
                    f"Only {curated_count}/{target_count} clips were curated. "
                    f"Upload completed for the available clips."
                )
            logger.warning(msg)
            db.record_daily_run_finish(
                run_id=run_id,
                status="partial",
                uploaded=uploaded_slots,
                scheduled=scheduled_slots,
                failed=failed_slots,
                message=msg,
            )
            send_telegram_alert(
                "DAILY_RUN_PARTIAL",
                f"<b>⚠️ Daily 100B Run PARTIAL</b>\n\n"
                f"<b>Date:</b> <code>{target_date_str}</code>\n"
                f"<b>Curation:</b> ⚠️ Partial ({curated_count}/{target_count} clips)\n"
                f"<b>Upload:</b> {'✅ Done' if upload_ok else '❌ Failed'}\n",
                {
                    "target_date": target_date_str,
                    "curated_count": curated_count,
                    "target_count": target_count,
                    "status": "partial",
                }
            )
            sys.exit(2)

        # Condition C: Full Success
        logger.success(f"Daily pipeline finished successfully for date: {target_date_str} ({curated_count}/{target_count} clips)!")
        db.record_daily_run_finish(
            run_id=run_id,
            status="success",
            uploaded=uploaded_slots,
            scheduled=scheduled_slots,
            failed=failed_slots,
            message="Completed successfully",
        )
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %I:%M %p IST")
        send_telegram_alert(
            "DAILY_RUN_COMPLETE",
            f"<b>🏆 Daily 100B Run Completed</b>\n\n"
            f"<b>Date:</b> <code>{target_date_str}</code> (Broadcast ET)\n"
            f"<b>Completed (IST):</b> <code>{now_ist}</code>\n"
            f"<b>Curation:</b> ✅ Complete ({curated_count}/{target_count} clips)\n"
            f"<b>Upload:</b> {'✅ Done' if upload_ok else '❌ Skipped'}\n",
            {
                "target_date": target_date_str,
                "curated_count": curated_count,
                "status": "success",
            }
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
