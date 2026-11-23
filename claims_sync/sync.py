"""
claims_sync/sync.py

Core sync engine for YouTube Content ID claims and video lifecycle:
  1. File lock to avoid concurrent sync executions
  2. Live probe: checks videos.list to ensure scheduled pipeline videos still exist on YouTube;
     marks missing videos as deleted, frees slot, and triggers curate.py --fill-slots
  3. Gmail scan: searches YouTube claim/strike emails, parses them idempotently,
     matches against uploads table (ignoring ~30 legacy channel videos),
     updates claims lifecycle, and executes safe auto-actions.
  4. Saves sync status for dashboard presentation.
"""

import os
import sys
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List
from loguru import logger

from claims_sync.auth import get_gmail_service
from claims_sync.client import search_youtube_claim_messages
from claims_sync.parser import parse_claim_email
from claims_sync.matcher import ClaimsMatcher
from claims_sync.actions import execute_claim_action
from claims_sync.config import load_claims_config
from agent.core.rights.alerts import notify_rights_alert

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
if str(WORKSPACE_ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT / "agent"))
DB_PATH = Path(os.environ.get("AGENT_DB_PATH", WORKSPACE_ROOT / "agent" / "data" / "agent.db"))
STATUS_FILE_PATH = WORKSPACE_ROOT / "agent" / "data" / "claims_sync_status.json"
LOCK_FILE_PATH = WORKSPACE_ROOT / "uploader" / "secrets" / "claims_sync.lock"


def _get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _get_youtube_service():
    """Load manage YouTube service for live existence checks and actions."""
    try:
        from dashboard.backend.youtube_api import get_youtube_service
        return get_youtube_service()
    except Exception as e:
        logger.warning(f"Could not initialize YouTube service: {e}")
        return None


def probe_missing_videos_on_youtube(youtube_service: Optional[Any] = None) -> List[Dict[str, Any]]:
    """
    Check if pipeline videos still exist on YouTube.
    
    Safety Rules:
      1. Circuit Breaker: If > 50% of clips appear missing, halt immediately and alert.
         Protects against erroneous API responses, wrong account/channel, or revoked token.
         Zero clips are marked deleted and zero slot fills are triggered.
      2. Future Dates Only: Only trigger curate.py --fill-slots for future dates/slots.
         Never trigger slot fills for past test dates or past broadcast days.
      3. Deduplicated Fills: Trigger curate.py --fill-slots at most once per unique future date.
    """
    if not youtube_service:
        logger.info("YouTube service not available; skipping live video existence probe.")
        return []

    with _get_db_connection() as conn:
        rows = conn.execute("""
            SELECT post_id, video_id, target_date, slot_time, status
            FROM uploads
            WHERE video_id IS NOT NULL
              AND status != 'deleted'
        """).fetchall()

    if not rows:
        return []

    video_map = {r["video_id"]: dict(r) for r in rows}
    video_ids = list(video_map.keys())
    total_videos = len(video_ids)

    # 1. Query YouTube in batches of 50 and collect all found IDs
    found_ids = set()
    for i in range(0, total_videos, 50):
        chunk = video_ids[i:i + 50]
        try:
            resp = youtube_service.videos().list(part="id,status", id=",".join(chunk)).execute()
            items = resp.get("items", []) if isinstance(resp, dict) else []
            for item in items:
                if isinstance(item, dict) and "id" in item:
                    found_ids.add(item["id"])
        except Exception as e:
            logger.error(f"Error querying YouTube videos.list during probe: {e}")
            notify_rights_alert("YouTube Probe API Error", f"Error querying videos.list: {e}")
            return []

    missing_ids = set(video_ids) - found_ids
    total_missing = len(missing_ids)

    if total_missing == 0:
        logger.info(f"Live YouTube probe: All {total_videos} pipeline videos verified present.")
        return []

    # 2. CIRCUIT BREAKER: Stop and alert if more than half the clips look missing
    if total_videos > 0 and total_missing > (total_videos / 2.0):
        halt_msg = (
            f"CRITICAL SAFETY STOP: YouTube existence probe reported {total_missing}/{total_videos} "
            f"({(total_missing / total_videos) * 100:.1f}%) pipeline videos as missing! "
            f"This exceeds the 50% safety limit (likely wrong channel/account, invalid token, or YouTube API anomaly). "
            f"Probe halted immediately. Zero clips were marked deleted and zero slot fills were triggered."
        )
        logger.critical(halt_msg)
        notify_rights_alert("CRITICAL: Missing Video Probe Safety Halt", halt_msg)
        return []

    # 3. Process confirmed missing videos (within safety threshold)
    logger.warning(f"Live probe: {total_missing}/{total_videos} video(s) confirmed missing on YouTube.")
    missing_records = []
    future_dates_to_fill = set()

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny_str = now_ny.strftime("%Y-%m-%d")

    for m_id in missing_ids:
        rec = video_map[m_id]
        missing_records.append(rec)
        logger.warning(f"Live probe: Video {m_id} (post {rec['post_id']}) is MISSING on YouTube! Marking deleted and freeing slot.")

        with _get_db_connection() as conn:
            # Mark deleted in uploads
            conn.execute("UPDATE uploads SET status = 'deleted' WHERE post_id = ?", (rec["post_id"],))
            # Mark empty in schedule
            conn.execute("UPDATE schedule SET status = 'empty' WHERE post_id = ?", (rec["post_id"],))

        # Check if slot/date is in the future
        t_date = rec.get("target_date")
        slot_time = rec.get("slot_time")
        is_future = False

        if t_date:
            if t_date > today_ny_str:
                is_future = True
            elif t_date == today_ny_str and slot_time:
                try:
                    slot_dt = datetime.fromisoformat(slot_time)
                    if slot_dt.tzinfo is None:
                        slot_dt = slot_dt.replace(tzinfo=ZoneInfo("America/New_York"))
                    if slot_dt > now_ny:
                        is_future = True
                except Exception:
                    pass

        if is_future:
            future_dates_to_fill.add(t_date)
        else:
            logger.info(f"Skipping slot fill for past date/slot: date={t_date}, slot={slot_time} (video {m_id})")

    # 4. Trigger curate.py --fill-slots for unique future dates only
    for fill_date in sorted(future_dates_to_fill):
        try:
            logger.info(f"Triggering curate.py --fill-slots {fill_date} for freed future slot(s)...")
            subprocess.Popen(
                [sys.executable, "curate.py", "--fill-slots", fill_date],
                cwd=str(WORKSPACE_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as ex:
            logger.warning(f"Failed to trigger slot fill for {fill_date}: {ex}")

    return missing_records


def run_claims_sync(
    limit: int = 50,
    youtube_service: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Main sync routine.
    """
    # 1. Lock file acquisition
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE_PATH.is_file():
        try:
            mtime = LOCK_FILE_PATH.stat().st_mtime
            if (datetime.now().timestamp() - mtime) < 1800:
                logger.warning("Another claims sync process is currently running. Exiting.")
                return {"success": False, "error": "Locked"}
        except Exception:
            pass

    with open(LOCK_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    try:
        cfg = load_claims_config()
        dry_run = cfg.get("dry_run", True)

        # 2. YouTube service init & probe
        yt_svc = youtube_service or _get_youtube_service()
        missing_videos = probe_missing_videos_on_youtube(yt_svc)

        # 3. Gmail service init
        gmail_status = "connected"
        gmail_error = None
        try:
            gmail_service = get_gmail_service()
        except Exception as e:
            gmail_status = "error"
            gmail_error = str(e)
            logger.error(f"Gmail authentication failed during claims sync: {e}")
            notify_rights_alert("Gmail Auth Failed", f"Claims sync halted because Gmail auth failed: {e}")
            _save_sync_status(
                gmail_status="error",
                gmail_error=gmail_error,
                processed_count=0,
                unmatched_count=0,
                dry_run=dry_run,
            )
            return {"success": False, "error": f"Gmail auth error: {e}"}

        # 4. Search YouTube claim messages
        messages = search_youtube_claim_messages(gmail_service, max_results=limit)
        logger.info(f"Retrieved {len(messages)} claim candidate message(s) from Gmail.")

        from core.db import AgentDatabase
        db = AgentDatabase(DB_PATH)
        matcher = ClaimsMatcher(db)

        processed_count = 0
        unmatched_count = 0
        actions_taken = []

        for msg in messages:
            msg_id = msg["id"]
            if matcher.is_email_processed(msg_id):
                continue

            parsed = parse_claim_email(
                message_id=msg_id,
                subject=msg.get("subject", ""),
                body_text=msg.get("body_text", ""),
                body_html=msg.get("body_html", ""),
                snippet=msg.get("snippet", ""),
            )

            # Ignore noise emails (newsletters, etc.)
            if parsed.is_noise:
                matcher.record_processed_email(
                    parsed=parsed,
                    received_at=msg.get("date", datetime.now().isoformat()),
                    subject=msg.get("subject", ""),
                    sender=msg.get("from", ""),
                    status="noise",
                )
                continue

            # Match against pipeline uploads table
            upload = matcher.match_email_to_upload(parsed)

            if not upload:
                # Video not in uploads table (could be older legacy channel video)
                logger.info(f"Email '{msg.get('subject')}' (id={msg_id}) did not match any pipeline upload.")
                unmatched_count += 1
                matcher.record_processed_email(
                    parsed=parsed,
                    received_at=msg.get("date", datetime.now().isoformat()),
                    subject=msg.get("subject", ""),
                    sender=msg.get("from", ""),
                    status="unmatched",
                )
                continue

            # Upload matched! Upsert claim in claims table
            claim_id, action_taken = matcher.upsert_claim(upload, parsed, source="email")
            matcher.record_processed_email(
                parsed=parsed,
                received_at=msg.get("date", datetime.now().isoformat()),
                subject=msg.get("subject", ""),
                sender=msg.get("from", ""),
                matched_post_id=upload["post_id"],
                matched_video_id=upload.get("video_id"),
                status="matched",
            )
            processed_count += 1

            # If claim added or strike, trigger policy action
            if parsed.event_type in ("claim_added", "strike"):
                act_res = execute_claim_action(
                    upload=upload,
                    claim_id=claim_id,
                    parsed=parsed,
                    youtube_service=yt_svc,
                )
                actions_taken.append({"post_id": upload["post_id"], "result": act_res})

        # 5. Save Status Report
        _save_sync_status(
            gmail_status="connected",
            gmail_error=None,
            processed_count=processed_count,
            unmatched_count=unmatched_count,
            dry_run=dry_run,
        )

        return {
            "success": True,
            "processed_count": processed_count,
            "unmatched_count": unmatched_count,
            "missing_videos_detected": len(missing_videos),
            "actions_taken": actions_taken,
        }

    finally:
        try:
            if LOCK_FILE_PATH.is_file():
                LOCK_FILE_PATH.unlink()
        except Exception:
            pass


def _save_sync_status(
    gmail_status: str,
    gmail_error: Optional[str],
    processed_count: int,
    unmatched_count: int,
    dry_run: bool,
) -> None:
    """Save sync status to agent/data/claims_sync_status.json."""
    status_data = {
        "last_sync_at": datetime.now().isoformat(),
        "gmail_connected": gmail_status == "connected",
        "gmail_error": gmail_error,
        "processed_count": processed_count,
        "unmatched_count": unmatched_count,
        "dry_run": dry_run,
    }
    try:
        STATUS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write sync status file: {e}")
