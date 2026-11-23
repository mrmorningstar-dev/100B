"""
claims_sync/actions.py

Automatic action policy engine for YouTube Content ID claims and copyright strikes.
Enforces:
  1. Gating via dry_run (simulate deletion, record in audit log, notify operator)
  2. Scheduled clips: Apply reversible hold (private, no publishAt) immediately even in dry-run
  3. Public clips: Delete only when dry-run is False and policy permits
  4. Daily cap: Maximum auto-actions per calendar day (default 6)
  5. Safeguard: Copyright strike emails raise urgent alerts and NEVER auto-delete
  6. Reserve replacement: Replace scheduled clips from reserve pool
  7. Empty slot fill: If no reserve clip available, hold claimed clip, alert, and trigger curate.py --fill-slots <date>
  8. Learning loop: Suggest blocklist additions (track, artist, claimant, uploader)
  9. Undo auto-hold: Restore clip from reversible hold
"""

import os
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, Tuple
from loguru import logger

from claims_sync.config import load_claims_config
from agent.core.rights.alerts import notify_rights_alert

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("AGENT_DB_PATH", WORKSPACE_ROOT / "agent" / "data" / "agent.db"))
SUGGESTIONS_PATH = WORKSPACE_ROOT / "agent" / "data" / "suggested_blocklist.json"


def _get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _format_slot_ist(slot_val: Optional[str]) -> str:
    """Format an ET slot time ISO or string into IST for human-readable notifications."""
    if not slot_val:
        return ""
    try:
        dt = datetime.fromisoformat(slot_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        dt_ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
        return f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
    except Exception:
        return f"{slot_val}"


def count_today_auto_actions(conn: sqlite3.Connection) -> int:
    """Count non-dry-run auto actions executed today."""
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM claim_actions
        WHERE dry_run = 0
          AND DATE(created_at) = DATE('now')
          AND action_type NOT IN ('alert_strike', 'undo_auto_hold')
    """).fetchone()
    return row["cnt"] if row else 0


def log_claim_action(
    claim_id: Optional[int],
    post_id: str,
    video_id: Optional[str],
    action_type: str,
    reason: str,
    dry_run: bool,
    result: str,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """Audit log entry in claim_actions table."""
    with _get_db_connection() as conn:
        cur = conn.execute("""
            INSERT INTO claim_actions (
                claim_id, post_id, video_id, action_type, reason,
                dry_run, result, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            claim_id,
            post_id,
            video_id,
            action_type,
            reason,
            1 if dry_run else 0,
            result,
            json.dumps(details or {}),
        ))
        return cur.lastrowid


def suggest_blocklist_additions(
    claimant: Optional[str],
    track: Optional[str],
    artist: Optional[str],
    post_id: str,
    uploader: Optional[str] = None,
) -> None:
    """
    Learning loop: suggest blocklist additions for confirmed claims.
    Appends to agent/data/suggested_blocklist.json.
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "post_id": post_id,
        "claimant": claimant,
        "track": track,
        "artist": artist,
        "uploader": uploader,
    }

    try:
        suggestions = []
        if SUGGESTIONS_PATH.is_file():
            with open(SUGGESTIONS_PATH, "r", encoding="utf-8") as f:
                suggestions = json.load(f)
        suggestions.append(entry)
        with open(SUGGESTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(suggestions, f, indent=2)
        logger.info(f"Learning loop: suggested blocklist additions recorded for post_id={post_id}")
    except Exception as e:
        logger.warning(f"Failed to record blocklist suggestions: {e}")


def determine_clip_scheduled_state(
    upload: Dict[str, Any],
    youtube_service: Optional[Any] = None,
) -> Tuple[bool, str]:
    """
    Determine whether a clip is currently scheduled (pending future publication) vs public (already live).

    CRITICAL RULE:
    Do NOT rely on uploads.status == 'scheduled'. In the pipeline database, uploads.status stays
    'scheduled' even after the clip has gone live on YouTube at its scheduled publishAt time.
    
    Decision Hierarchy:
    1. Live YouTube status (if youtube_service and video_id are available):
       - If live privacyStatus == "public": DEFINITIVELY PUBLIC.
       - If live privacyStatus == "private" and status.publishAt:
           Compare publishAt to now_utc:
           - publishAt > now_utc: SCHEDULED (future broadcast).
           - publishAt <= now_utc: PUBLIC (publish time arrived).
       - If live privacyStatus == "private" and no publishAt:
           Check database timing fields below.
    
    2. Local database publish_at (RFC3339 UTC string):
       - If present:
           - publish_at > now_utc: SCHEDULED.
           - publish_at <= now_utc: PUBLIC (publish time already arrived).
    
    3. Local database slot_time (America/New_York ISO string):
       - If present:
           - slot_time > now_utc: SCHEDULED.
           - slot_time <= now_utc: PUBLIC.
           
    4. Local database privacy column:
       - If privacy == "public": PUBLIC.
    
    5. Local database target_date (YYYY-MM-DD):
       - If target_date > today_ny: SCHEDULED.
       - If target_date < today_ny: PUBLIC.
    """
    now_utc = datetime.now(timezone.utc)
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny_str = now_ny.strftime("%Y-%m-%d")

    video_id = upload.get("video_id")
    privacy = (upload.get("privacy") or "").lower()
    publish_at = upload.get("publish_at")
    slot_time = upload.get("slot_time")
    target_date = upload.get("target_date")

    # 1. Live YouTube status check (most authoritative)
    if youtube_service and video_id:
        try:
            v_resp = youtube_service.videos().list(part="status", id=video_id).execute()
            if isinstance(v_resp, dict):
                items = v_resp.get("items", [])
                if isinstance(items, list) and len(items) > 0 and isinstance(items[0], dict):
                    yt_status = items[0].get("status", {})
                    yt_privacy = (yt_status.get("privacyStatus") or "").lower()
                    yt_publish_at = yt_status.get("publishAt")

                    if yt_privacy == "public":
                        return False, f"Live YouTube status is 'public' (video_id: {video_id})"

                    if yt_publish_at:
                        try:
                            pub_dt = datetime.fromisoformat(yt_publish_at.replace("Z", "+00:00"))
                            if pub_dt.tzinfo is None:
                                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                            else:
                                pub_dt = pub_dt.astimezone(timezone.utc)

                            if pub_dt > now_utc:
                                return True, f"Live YouTube publishAt is in the future: {yt_publish_at}"
                            else:
                                return False, f"Live YouTube publishAt has passed: {yt_publish_at}"
                        except Exception as ex:
                            logger.warning(f"Failed parsing live YouTube publishAt '{yt_publish_at}': {ex}")
        except Exception as e:
            logger.warning(f"Could not verify live YouTube status for {video_id}: {e}")

    # 2. Database publish_at (UTC)
    if publish_at:
        try:
            pub_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            else:
                pub_dt = pub_dt.astimezone(timezone.utc)

            if pub_dt > now_utc:
                return True, f"Database publish_at is in the future: {publish_at}"
            else:
                return False, f"Database publish_at has passed: {publish_at}"
        except Exception as ex:
            logger.warning(f"Failed parsing database publish_at '{publish_at}': {ex}")

    # 3. Database slot_time (America/New_York)
    if slot_time:
        try:
            slot_dt = datetime.fromisoformat(slot_time)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            slot_dt_utc = slot_dt.astimezone(timezone.utc)

            if slot_dt_utc > now_utc:
                return True, f"Database slot_time is in the future: {slot_time}"
            else:
                return False, f"Database slot_time has passed: {slot_time}"
        except Exception as ex:
            logger.warning(f"Failed parsing database slot_time '{slot_time}': {ex}")

    # 4. Database privacy setting
    if privacy == "public":
        return False, "Database privacy is 'public'"

    # 5. Database target_date comparison
    if target_date:
        if target_date > today_ny_str:
            return True, f"Target date is in the future: {target_date} > {today_ny_str}"
        elif target_date < today_ny_str:
            return False, f"Target date is in the past: {target_date} < {today_ny_str}"

    # Default fallback: Treat as public rather than mistakenly replacing a live clip
    return False, "Default fallback: treat as public (publish time indeterminate)"


def execute_claim_action(
    upload: Dict[str, Any],
    claim_id: int,
    parsed: Any,
    youtube_service: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Execute policy for an active claim or strike on an upload.
    """
    post_id = upload["post_id"]
    video_id = upload.get("video_id")
    target_date = upload.get("target_date")
    title = upload.get("title", f"Video {post_id}")
    status = upload.get("status", "scheduled")
    publish_at = upload.get("publish_at")

    config = load_claims_config()
    dry_run = config.get("dry_run", True)
    max_actions_per_day = config.get("max_auto_actions_per_day", 6)

    # 1. STRIKE SAFEGUARD: Strikes are NEVER auto-deleted; immediate urgent alert
    if parsed.is_strike:
        msg = f"URGENT COPYRIGHT STRIKE on video '{title}' (ID: {video_id}, post: {post_id})! Immediate manual review required in YouTube Studio."
        logger.critical(msg)
        notify_rights_alert("CRITICAL: Copyright Strike Detected", msg)
        log_claim_action(
            claim_id=claim_id,
            post_id=post_id,
            video_id=video_id,
            action_type="alert_strike",
            reason="Copyright strike received from YouTube",
            dry_run=False,
            result="strike_escalated",
            details={"is_strike": True, "title": title},
        )
        return {"action": "alert_strike", "is_strike": True, "success": True}

    # 2. Check Daily Action Cap
    with _get_db_connection() as conn:
        today_count = count_today_auto_actions(conn)

    if today_count >= max_actions_per_day and not dry_run:
        msg = f"Daily auto-action cap reached ({today_count}/{max_actions_per_day}). Auto-deletion paused for video '{title}' ({video_id})."
        logger.warning(msg)
        notify_rights_alert("Daily Auto-Action Cap Reached", msg)
        log_claim_action(
            claim_id=claim_id,
            post_id=post_id,
            video_id=video_id,
            action_type="hold_capped",
            reason=f"Exceeded max_auto_actions_per_day ({max_actions_per_day})",
            dry_run=False,
            result="cap_exceeded",
        )
        return {"action": "capped", "success": False, "error": "Daily cap exceeded"}

    # Determine if video is scheduled (pending future publication) vs public (already live)
    # Never rely on status == "scheduled", as uploads.status stays "scheduled" after going public
    is_scheduled, timing_reason = determine_clip_scheduled_state(upload, youtube_service=youtube_service)
    logger.info(f"Clip timing classification for {post_id} ({video_id}): is_scheduled={is_scheduled} ({timing_reason})")

    # 3. SCHEDULED CLIPS POLICY
    if is_scheduled:
        # Constraint: Even in dry-run, apply the reversible hold (private, no publishAt)
        # to prevent claimed videos from accidentally going live
        from agent.core.rights.youtube_gate import YouTubeRightsGate
        gate = YouTubeRightsGate(db_path=DB_PATH)

        logger.info(f"Applying reversible hold to claimed scheduled video: {post_id} ({video_id})")
        hold_res = gate.mark_held(post_id=post_id, youtube_service=youtube_service)

        # Trigger learning loop suggestion
        suggest_blocklist_additions(
            claimant=parsed.claimant,
            track=parsed.asset_title,
            artist=parsed.track_artist,
            post_id=post_id,
            uploader=upload.get("uploader"),
        )

        if dry_run:
            # DRY-RUN: Simulation only. Deletion is not performed.
            sim_msg = (
                f"[DRY-RUN] Claim detected on scheduled video '{title}' ({video_id}). "
                f"Reversible hold applied. Planned action: delete and replace from reserve pool."
            )
            logger.info(sim_msg)
            notify_rights_alert("Claims Sync [DRY-RUN]", sim_msg)
            log_claim_action(
                claim_id=claim_id,
                post_id=post_id,
                video_id=video_id,
                action_type="simulate_delete_and_replace",
                reason=f"Claim on scheduled video by {parsed.claimant or 'Unknown'} ({parsed.asset_title or 'Unknown'})",
                dry_run=True,
                result="simulated_dry_run",
                details={"planned_action": "delete_and_replace_from_reserve", "hold_result": hold_res},
            )
            return {"action": "simulate_delete_and_replace", "dry_run": True, "held": True, "success": True}

        else:
            # LIVE RUN (dry_run=False): Delete and replace from reserve
            logger.info(f"Live auto-action: replacing claimed video {post_id} ({video_id}) from reserve pool...")
            replace_res = gate.replace_from_reserve(
                claimed_post_id=post_id,
                action_on_claimed="delete",
                delete_confirmation="DELETE",
                youtube_service=youtube_service,
            )

            if not replace_res.get("success"):
                # No reserve clip available: hold claimed clip, alert, and trigger curate.py --fill-slots
                err = replace_res.get("error", "Unknown reserve replacement error")
                warn_msg = f"No reserve clip available to replace {post_id}: {err}. Video held private. Triggering slot fill."
                logger.warning(warn_msg)
                notify_rights_alert("No Reserve Clip Available", warn_msg)

                # Trigger fill-slots in background if date known
                if target_date:
                    try:
                        logger.info(f"Spawning curate.py --fill-slots {target_date}...")
                        subprocess.Popen(
                            ["python", "curate.py", "--fill-slots", target_date],
                            cwd=str(WORKSPACE_ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception as ex:
                        logger.warning(f"Could not spawn curate.py --fill-slots: {ex}")

                log_claim_action(
                    claim_id=claim_id,
                    post_id=post_id,
                    video_id=video_id,
                    action_type="hold_slot_empty",
                    reason=f"Claim on scheduled video; reserve pool empty: {err}",
                    dry_run=False,
                    result="held_empty_slot_fill_triggered",
                    details={"error": err},
                )
                return {"action": "held_empty_slot", "dry_run": False, "success": False, "error": err}

            # Successfully replaced from reserve
            slot_raw = replace_res.get('slot_time')
            slot_ist_str = f" ({_format_slot_ist(slot_raw)})" if slot_raw else ""
            success_msg = f"Auto-Action: Deleted claimed video {video_id} and replaced with {replace_res.get('new_post_id')} in slot {slot_raw}{slot_ist_str}."
            logger.info(success_msg)
            notify_rights_alert("Claimed Video Replaced", success_msg)
            log_claim_action(
                claim_id=claim_id,
                post_id=post_id,
                video_id=video_id,
                action_type="delete_and_replace",
                reason=f"Claim on scheduled video by {parsed.claimant or 'Unknown'} ({parsed.asset_title or 'Unknown'})",
                dry_run=False,
                result="success",
                details=replace_res,
            )
            return {"action": "delete_and_replace", "dry_run": False, "success": True, "details": replace_res}

    # 4. PUBLIC CLIPS POLICY
    else:
        # Public clip already published
        public_action = config.get("auto_action_public", "delete")

        # Record learning suggestion
        suggest_blocklist_additions(
            claimant=parsed.claimant,
            track=parsed.asset_title,
            artist=parsed.track_artist,
            post_id=post_id,
            uploader=upload.get("uploader"),
        )

        if dry_run:
            sim_msg = f"[DRY-RUN] Claim detected on public video '{title}' ({video_id}). Planned action: {public_action}."
            logger.info(sim_msg)
            notify_rights_alert("Claims Sync [DRY-RUN]", sim_msg)
            log_claim_action(
                claim_id=claim_id,
                post_id=post_id,
                video_id=video_id,
                action_type=f"simulate_{public_action}_public",
                reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                dry_run=True,
                result="simulated_dry_run",
            )
            return {"action": f"simulate_{public_action}_public", "dry_run": True, "success": True}

        else:
            if public_action == "delete":
                # Check keep_if_views_over
                views_cap = config.get("keep_if_views_over")
                if views_cap and youtube_service and video_id:
                    try:
                        v_resp = youtube_service.videos().list(part="statistics", id=video_id).execute()
                        items = v_resp.get("items", [])
                        if items:
                            views = int(items[0].get("statistics", {}).get("viewCount", 0))
                            if views >= views_cap:
                                skip_msg = f"Public video '{title}' has {views} views (>= {views_cap}). Skipping auto-delete; keeping with claim."
                                logger.warning(skip_msg)
                                notify_rights_alert("High-Traffic Claimed Video Kept", skip_msg)
                                log_claim_action(
                                    claim_id=claim_id,
                                    post_id=post_id,
                                    video_id=video_id,
                                    action_type="keep_high_views",
                                    reason=f"Views {views} >= threshold {views_cap}",
                                    dry_run=False,
                                    result="kept_high_views",
                                )
                                return {"action": "keep_high_views", "views": views, "success": True}
                    except Exception as e:
                        logger.warning(f"Could not check view count for {video_id}: {e}")

                # Delete public video on YouTube
                if not youtube_service:
                    err_msg = f"Cannot delete public claimed video '{title}' ({video_id}): YouTube service is not available."
                    logger.error(err_msg)
                    notify_rights_alert("CRITICAL: Failed to Delete Public Claimed Video", err_msg)
                    log_claim_action(
                        claim_id=claim_id,
                        post_id=post_id,
                        video_id=video_id,
                        action_type="delete_public",
                        reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                        dry_run=False,
                        result="failed",
                        details={"error": err_msg, "title": title},
                    )
                    return {"action": "delete_public", "dry_run": False, "success": False, "error": err_msg}

                if not video_id:
                    err_msg = f"Cannot delete public claimed video for {post_id}: video_id is missing."
                    logger.error(err_msg)
                    log_claim_action(
                        claim_id=claim_id,
                        post_id=post_id,
                        video_id=video_id,
                        action_type="delete_public",
                        reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                        dry_run=False,
                        result="failed",
                        details={"error": err_msg, "title": title},
                    )
                    return {"action": "delete_public", "dry_run": False, "success": False, "error": err_msg}

                yt_deleted = False
                delete_error = None
                try:
                    youtube_service.videos().delete(id=video_id).execute()
                    yt_deleted = True
                    logger.info(f"Successfully deleted public claimed video {video_id} on YouTube.")
                except Exception as e:
                    err_str = str(e).lower()
                    if "404" in err_str or "notfound" in err_str or "not found" in err_str:
                        yt_deleted = True
                        logger.info(f"Public video {video_id} was already removed on YouTube (404 Not Found).")
                    else:
                        delete_error = str(e)
                        logger.error(f"Failed to delete public video {video_id} on YouTube: {e}")

                if not yt_deleted:
                    fail_msg = (
                        f"CRITICAL: Failed to delete public claimed video '{title}' ({video_id}) on YouTube! "
                        f"Error: {delete_error}. Video remains LIVE on YouTube. Not marked deleted in database."
                    )
                    logger.critical(fail_msg)
                    notify_rights_alert("CRITICAL: YouTube Deletion Failed", fail_msg)
                    log_claim_action(
                        claim_id=claim_id,
                        post_id=post_id,
                        video_id=video_id,
                        action_type="delete_public",
                        reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                        dry_run=False,
                        result="failed",
                        details={"error": delete_error, "title": title},
                    )
                    return {
                        "action": "delete_public",
                        "dry_run": False,
                        "success": False,
                        "error": delete_error,
                    }

                # Only mark deleted in SQLite if YouTube deletion succeeded
                with _get_db_connection() as conn:
                    conn.execute("UPDATE uploads SET status = 'deleted' WHERE post_id = ?", (post_id,))

                del_msg = f"Auto-Action: Successfully deleted public claimed video '{title}' ({video_id}) on YouTube."
                logger.info(del_msg)
                notify_rights_alert("Public Claimed Video Deleted", del_msg)
                log_claim_action(
                    claim_id=claim_id,
                    post_id=post_id,
                    video_id=video_id,
                    action_type="delete_public",
                    reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                    dry_run=False,
                    result="success",
                )
                return {"action": "delete_public", "dry_run": False, "success": True}

            elif public_action == "private":
                if not youtube_service:
                    err_msg = f"Cannot set public claimed video '{title}' ({video_id}) to private: YouTube service is not available."
                    logger.error(err_msg)
                    notify_rights_alert("CRITICAL: Failed to Private Public Claimed Video", err_msg)
                    log_claim_action(
                        claim_id=claim_id,
                        post_id=post_id,
                        video_id=video_id,
                        action_type="private_public",
                        reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                        dry_run=False,
                        result="failed",
                        details={"error": err_msg, "title": title},
                    )
                    return {"action": "private_public", "dry_run": False, "success": False, "error": err_msg}

                yt_privated = False
                update_error = None
                try:
                    youtube_service.videos().update(
                        part="status",
                        body={"id": video_id, "status": {"privacyStatus": "private"}},
                    ).execute()
                    yt_privated = True
                    logger.info(f"Made public claimed video {video_id} private on YouTube.")
                except Exception as e:
                    update_error = str(e)
                    logger.error(f"Failed to update video {video_id} privacy to private on YouTube: {e}")

                if not yt_privated:
                    fail_msg = f"Failed to set public video '{title}' ({video_id}) to private on YouTube: {update_error}."
                    logger.critical(fail_msg)
                    notify_rights_alert("CRITICAL: Failed to Private Public Claimed Video", fail_msg)
                    log_claim_action(
                        claim_id=claim_id,
                        post_id=post_id,
                        video_id=video_id,
                        action_type="private_public",
                        reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                        dry_run=False,
                        result="failed",
                        details={"error": update_error, "title": title},
                    )
                    return {"action": "private_public", "dry_run": False, "success": False, "error": update_error}

                with _get_db_connection() as conn:
                    conn.execute("UPDATE uploads SET privacy = 'private' WHERE post_id = ?", (post_id,))

                log_claim_action(
                    claim_id=claim_id,
                    post_id=post_id,
                    video_id=video_id,
                    action_type="private_public",
                    reason=f"Claim on public video by {parsed.claimant or 'Unknown'}",
                    dry_run=False,
                    result="success",
                )
                return {"action": "private_public", "dry_run": False, "success": True}

            else:
                return {"action": "none", "dry_run": False, "success": True}


def undo_auto_hold(post_id: str, youtube_service: Optional[Any] = None) -> Dict[str, Any]:
    """
    Undo reversible auto-hold for a post_id:
      - Restores scheduled publishAt (or next free slot)
      - Sets status to cleared
      - Audit logs the action
    """
    from agent.core.rights.youtube_gate import YouTubeRightsGate
    gate = YouTubeRightsGate(db_path=DB_PATH)

    res = gate.mark_clear(post_id=post_id, youtube_service=youtube_service)

    if res.get("success"):
        with _get_db_connection() as conn:
            conn.execute("UPDATE uploads SET claim_status = 'cleared' WHERE post_id = ?", (post_id,))

        log_claim_action(
            claim_id=None,
            post_id=post_id,
            video_id=res.get("video_id"),
            action_type="undo_auto_hold",
            reason="Operator manual undo auto-hold",
            dry_run=False,
            result="success",
            details=res,
        )
        logger.info(f"Undo auto-hold successful for {post_id}.")

    return res
