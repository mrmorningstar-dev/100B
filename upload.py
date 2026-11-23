#!/usr/bin/env python3
"""
==============================================================================
               100B VIDEO BOT — PHASE 4 YOUTUBE UPLOADER CLI
==============================================================================

CLI commands:
    python upload.py auth
    python upload.py upload --date tomorrow [--dry-run] [--confirm-public]
    python upload.py run-due [--dry-run] [--confirm-public]
    python upload.py verify --date tomorrow
    python upload.py status [--days 7]
    python upload.py retry-failed --date tomorrow
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger
import yaml

from rich.console import Console
from rich.table import Table

from uploader.auth import authenticate_interactive, get_authenticated_service
from uploader.youtube_client import YouTubeUploader
from uploader.db import UploadDatabase, update_schedule_slot
from uploader.alerts import send_telegram_alert, send_upload_summary


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "uploader" / "config.yaml"


def load_uploader_config() -> Dict[str, Any]:
    if DEFAULT_CONFIG_PATH.is_file():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


from uploader.dates import resolve_date_arg, get_ist_today


def cmd_auth(args):
    """Run one-time interactive OAuth2 authorization."""
    is_manage = getattr(args, "manage", False)
    logger.info(f"Starting one-time YouTube OAuth authorization (manage={is_manage})...")
    try:
        authenticate_interactive(manage=is_manage)
        token_name = "token_manage.json" if is_manage else "token.json"
        print(f"\n[SUCCESS] YouTube OAuth authorized! {token_name} has been saved in uploader/secrets/.")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        sys.exit(1)


def cmd_upload(args):
    """Upload or schedule videos from schedule.json for a specific date."""
    config = load_uploader_config()
    tz_name = config.get("timezone", "America/New_York")
    target_date = resolve_date_arg(args.date, tz_name)

    logger.info(f"--- [Phase 4] YouTube Upload Runner | Date: {target_date} (TZ: {tz_name}) ---")
    schedule_path = BASE_DIR / "agent" / "data" / "edited" / target_date / "schedule.json"

    if not schedule_path.is_file():
        logger.error(f"Schedule file not found: {schedule_path}")
        sys.exit(1)

    with open(schedule_path, "r", encoding="utf-8") as f:
        schedule_data = json.load(f)

    slots = schedule_data.get("slots", [])
    if not slots:
        logger.error(f"No slots found in {schedule_path}")
        sys.exit(1)

    db = UploadDatabase()
    service = None if args.dry_run else get_authenticated_service()
    uploader = YouTubeUploader(youtube_service=service, config=config, db=db)

    mode = args.mode or config.get("upload", {}).get("mode", "scheduled")
    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        logger.info(f"Upload limit set: uploading at most {limit} fresh slot(s)")

    uploaded_cnt = 0
    scheduled_cnt = 0
    failed_cnt = 0
    fresh_count = 0
    errors = []

    for slot in slots:
        post_id = slot.get("post_id", "unknown")
        slot_time = slot.get("slot_time", "")

        existing_record = db.get_upload(post_id)
        is_already_done = existing_record and existing_record.get("status") in ("uploaded", "scheduled")

        if not is_already_done and limit is not None and fresh_count >= limit:
            logger.info(f"Reached upload limit ({limit}); skipping remaining slots.")
            break

        status, video_id, err = uploader.upload_slot(
            slot=slot,
            target_date=target_date,
            schedule_path=schedule_path,
            confirm_public=args.confirm_public,
            mode=mode,
            dry_run=args.dry_run,
        )

        if not is_already_done:
            fresh_count += 1

        if status == "scheduled":
            scheduled_cnt += 1
        elif status == "uploaded":
            uploaded_cnt += 1
        elif status in ("failed", "pending"):
            if status == "failed":
                failed_cnt += 1
            errors.append({"post_id": post_id, "slot_time": slot_time, "error": err or status})

        if limit is not None and fresh_count >= limit:
            logger.info(f"Reached upload limit ({limit}); stopping further uploads.")
            break

    # Summary notification
    send_upload_summary(
        target_date=target_date,
        total_slots=len(slots),
        uploaded_count=uploaded_cnt,
        scheduled_count=scheduled_cnt,
        failed_count=failed_cnt,
        mode=mode,
        quota_used=uploader.quota.used_today,
        dry_run=args.dry_run,
        errors=errors,
    )

    logger.info(
        f"Upload finished for {target_date} -> Scheduled: {scheduled_cnt} | "
        f"Uploaded: {uploaded_cnt} | Failed: {failed_cnt} | Quota: {uploader.quota.used_today} units"
    )

    if failed_cnt > 0:
        logger.error(f"{failed_cnt} slot(s) failed upload for date {target_date}.")
        sys.exit(1)


def cmd_run_due(args):
    """Realtime runner: scan today's schedule for slots that are due now."""
    config = load_uploader_config()
    tz_name = config.get("timezone", "America/New_York")
    today = resolve_date_arg("today", tz_name)

    schedule_path = BASE_DIR / "agent" / "data" / "edited" / today / "schedule.json"
    if not schedule_path.is_file():
        logger.info(f"No schedule file for today ({today}); nothing due.")
        return

    with open(schedule_path, "r", encoding="utf-8") as f:
        schedule_data = json.load(f)

    slots = schedule_data.get("slots", [])
    db = UploadDatabase()
    service = None if args.dry_run else get_authenticated_service()
    uploader = YouTubeUploader(youtube_service=service, config=config, db=db)

    now_utc = datetime.now(timezone.utc)
    max_late = int(config.get("upload", {}).get("max_late_minutes", 30))

    due_slots = []
    for s in slots:
        status = s.get("status", "queued")
        if status in ("uploaded", "scheduled"):
            continue

        slot_time_str = s.get("slot_time", "")
        try:
            slot_dt_utc = uploader.parse_slot_time_utc(slot_time_str)
        except Exception:
            continue

        # Due window: slot_time <= now_utc and now_utc <= slot_time + max_late
        if slot_dt_utc <= now_utc and now_utc <= (slot_dt_utc + timedelta(minutes=max_late)):
            due_slots.append(s)

    if not due_slots:
        logger.info(f"No slots due right now for date {today}.")
        return

    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        due_slots = due_slots[:limit]
        logger.info(f"Applying limit {limit}: uploading up to {len(due_slots)} due slot(s)...")
    else:
        logger.info(f"Found {len(due_slots)} due slot(s) for realtime upload...")

    for s in due_slots:
        uploader.upload_slot(
            slot=s,
            target_date=today,
            schedule_path=schedule_path,
            confirm_public=args.confirm_public,
            mode="realtime",
            dry_run=args.dry_run,
        )


def cmd_verify(args):
    """
    Post-upload verification: query YouTube for processing details.
    Flags rejected or removed videos as 'needs_review' and alerts.
    Does NOT treat 'still processing' / 'processing' as a failure.
    """
    config = load_uploader_config()
    tz_name = config.get("timezone", "America/New_York")
    target_date = resolve_date_arg(args.date, tz_name)

    logger.info(f"Verifying YouTube upload status for date: {target_date}...")
    db = UploadDatabase()
    uploads = db.get_uploads_by_date(target_date)

    if not uploads:
        logger.warning(f"No upload records found in database for date: {target_date}")
        return

    service = get_authenticated_service()
    uploader = YouTubeUploader(youtube_service=service, config=config, db=db)
    schedule_path = BASE_DIR / "agent" / "data" / "edited" / target_date / "schedule.json"

    needs_review_count = 0
    ok_count = 0

    for up in uploads:
        video_id = up.get("video_id")
        post_id = up.get("post_id")
        if not video_id or video_id.startswith("dry_run"):
            continue

        try:
            uploader.quota.record_read()
            resp = service.videos().list(
                part="status,processingDetails,contentDetails",
                id=video_id,
            ).execute()

            items = resp.get("items", [])
            if not items:
                msg = f"Video {video_id} ({post_id}) not found on YouTube! Might have been removed."
                logger.error(msg)
                db.update_status(post_id, "needs_review", error="video_not_found_on_channel")
                if schedule_path.is_file():
                    update_schedule_slot(schedule_path, post_id, {"status": "needs_review", "error": msg})
                send_telegram_alert("VIDEO_NOT_FOUND", msg, {"video_id": video_id, "post_id": post_id})
                needs_review_count += 1
                continue

            v_status = items[0].get("status", {})
            p_details = items[0].get("processingDetails", {})

            upload_status = v_status.get("uploadStatus")
            rejection_reason = v_status.get("rejectionReason")
            processing_status = p_details.get("processingStatus")

            logger.info(
                f"Video {video_id} ({post_id}): uploadStatus='{upload_status}', "
                f"processingStatus='{processing_status}', rejectionReason='{rejection_reason}'"
            )

            # Check for scheduled uploads that lost publishAt (unaudited project restriction)
            target_publish_at = up.get("publish_at")
            if target_publish_at and not v_status.get("publishAt"):
                try:
                    slot_dt = datetime.fromisoformat(target_publish_at)
                    if datetime.now(timezone.utc) < slot_dt:
                        sched_msg = (
                            f"Scheduled video {video_id} ({post_id}) is missing status.publishAt "
                            f"(expected: {target_publish_at}). YouTube locked the video as a private draft."
                        )
                        logger.warning(sched_msg)
                        send_telegram_alert("UNAUDITED_PROJECT_LOCK", sched_msg, {
                            "video_id": video_id,
                            "post_id": post_id,
                            "expected_publish_at": target_publish_at,
                        })
                except Exception:
                    pass

            # Rejections & failures
            if upload_status in ("rejected", "failed") or rejection_reason:
                msg = f"Video {video_id} ({post_id}) rejected by YouTube: {rejection_reason or upload_status}"
                logger.error(msg)
                db.update_status(post_id, "needs_review", error=f"rejected: {rejection_reason or upload_status}")
                if schedule_path.is_file():
                    update_schedule_slot(schedule_path, post_id, {"status": "needs_review", "error": msg})
                send_telegram_alert("VIDEO_REJECTED", msg, {
                    "video_id": video_id,
                    "post_id": post_id,
                    "rejection_reason": rejection_reason,
                })
                needs_review_count += 1
            else:
                # Still processing or succeeded -> healthy!
                ok_count += 1

        except Exception as e:
            logger.warning(f"Error querying video {video_id}: {e}")

    logger.info(
        f"Verification complete for {target_date}: {ok_count} healthy / in-progress, "
        f"{needs_review_count} flagged needs_review. Quota reads: {uploader.quota.reads_today} units."
    )


def cmd_status(args):
    """Display rich status table of recent uploads and daily quota."""
    days = getattr(args, "days", 7)
    db = UploadDatabase()
    rows = db.get_recent_uploads(days=days)
    config = load_uploader_config()

    p_date = db.get_pacific_date()
    q_data = db.get_daily_quota(p_date)
    p_uploads = db.get_pacific_uploads_count(p_date)
    max_uploads = config.get("upload", {}).get("max_uploads_per_day", 6)
    daily_cap = config.get("quota", {}).get("daily_cap", 10000)

    console = Console()
    console.print(
        f"\n[bold]Pacific Date (PT):[/bold] [yellow]{p_date}[/yellow] | "
        f"[bold]Uploads Today:[/bold] [cyan]{p_uploads} / {max_uploads}[/cyan] | "
        f"[bold]Quota Used Today:[/bold] [magenta]{q_data.get('quota_used', 0):,} / {daily_cap:,} units[/magenta] "
        f"([dim]{q_data.get('inserts_count', 0)} inserts, {q_data.get('reads_count', 0)} reads[/dim])"
    )

    table = Table(title=f"YouTube Uploads — Last {days} Days", show_header=True, header_style="bold cyan")
    table.add_column("Slot Time", style="yellow")
    table.add_column("Post ID", style="magenta")
    table.add_column("Status", style="bold")
    table.add_column("Video ID", style="green")
    table.add_column("Privacy")
    table.add_column("Attempts", justify="right")
    table.add_column("Error / Notes", style="dim")

    for r in rows:
        status_style = "green" if r["status"] in ("uploaded", "scheduled") else ("yellow" if r["status"] in ("pending", "uploading") else "red")
        slot_raw = r.get("slot_time") or ""
        slot_display = slot_raw
        if slot_raw:
            try:
                s_dt = datetime.fromisoformat(slot_raw)
                if s_dt.tzinfo is None:
                    s_dt = s_dt.replace(tzinfo=ZoneInfo("America/New_York"))
                ist_dt = s_dt.astimezone(ZoneInfo("Asia/Kolkata"))
                slot_display = f"{slot_raw} ({ist_dt.strftime('%I:%M %p').lstrip('0')} IST)"
            except Exception:
                pass
        table.add_row(
            slot_display,
            r.get("post_id") or "",
            f"[{status_style}]{r.get('status')}[/{status_style}]",
            r.get("video_id") or "-",
            r.get("privacy") or "-",
            str(r.get("attempts", 0)),
            r.get("last_error") or "-",
        )

    console.print()
    console.print(table)
    console.print()


def cmd_retry_failed(args):
    """Retry any slots marked failed or pending for a given date."""
    args.mode = "scheduled"
    cmd_upload(args)


def main():
    parser = argparse.ArgumentParser(description="100B YouTube Shorts Upload Manager — Phase 4")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. auth
    p_auth = subparsers.add_parser("auth", help="Perform one-time interactive OAuth authorization")
    p_auth.add_argument("--manage", action="store_true", help="Authorize with youtube scope for dashboard management (saves to token_manage.json)")
    p_auth.set_defaults(func=cmd_auth)

    # 2. upload
    p_upload = subparsers.add_parser("upload", help="Upload or schedule a daily batch")
    p_upload.add_argument("--date", default="tomorrow", help="Target date ('tomorrow' or 'YYYY-MM-DD')")
    p_upload.add_argument("--dry-run", action="store_true", help="Simulate upload without calling YouTube API")
    p_upload.add_argument("--confirm-public", action="store_true", help="Confirm publishing publicly or with publishAt schedule")
    p_upload.add_argument("--mode", choices=["scheduled", "realtime"], default=None, help="Upload mode override")
    p_upload.add_argument("--limit", type=int, default=None, help="Maximum number of fresh slots to upload (e.g. 1)")
    p_upload.set_defaults(func=cmd_upload)

    # 3. run-due
    p_due = subparsers.add_parser("run-due", help="Realtime runner: upload slots that are due now")
    p_due.add_argument("--dry-run", action="store_true", help="Simulate upload")
    p_due.add_argument("--confirm-public", action="store_true", help="Confirm publishing publicly")
    p_due.add_argument("--limit", type=int, default=None, help="Maximum number of slots to upload")
    p_due.set_defaults(func=cmd_run_due)

    # 4. verify
    p_verify = subparsers.add_parser("verify", help="Check processing status of uploaded videos")
    p_verify.add_argument("--date", default="tomorrow", help="Target date ('tomorrow' or 'YYYY-MM-DD')")
    p_verify.set_defaults(func=cmd_verify)

    # 5. status
    p_status = subparsers.add_parser("status", help="Show recent upload status table")
    p_status.add_argument("--days", type=int, default=7, help="Number of days to display (default: 7)")
    p_status.set_defaults(func=cmd_status)

    # 6. retry-failed
    p_retry = subparsers.add_parser("retry-failed", help="Retry failed or pending uploads for a date")
    p_retry.add_argument("--date", default="tomorrow", help="Target date ('tomorrow' or 'YYYY-MM-DD')")
    p_retry.add_argument("--dry-run", action="store_true", help="Simulate retry")
    p_retry.add_argument("--confirm-public", action="store_true", help="Confirm public")
    p_retry.add_argument("--limit", type=int, default=None, help="Maximum number of slots to retry")
    p_retry.set_defaults(func=cmd_retry_failed)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
