import os
import re
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

from fastapi import APIRouter, Request, HTTPException, status
from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from dashboard.backend.db import get_ro_connection, WORKSPACE_DIR, update_local_retimed_data
import dashboard.backend.db as backend_db
from dashboard.backend.youtube_api import has_manage_scope, update_video_publish_time

router = APIRouter(prefix="/api/schedule", tags=["schedule"])

AGENT_CONFIG_PATH = WORKSPACE_DIR / "agent" / "config.yaml"
UPLOADER_CONFIG_PATH = WORKSPACE_DIR / "uploader" / "config.yaml"
SCHEDULE_DATA_DIR = WORKSPACE_DIR / "agent" / "data"


class ScheduleUpdateRequest(BaseModel):
    videos_per_day: int = Field(..., ge=1, le=12)
    slots: List[str]


def parse_hhmm_to_minutes(time_str: str) -> int:
    """Validate and convert 'HH:MM' string to minutes from midnight."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not m:
        raise ValueError(f"Invalid time format '{time_str}'. Expected 'HH:MM' (24-hour).")
    hours, minutes = int(m.group(1)), int(m.group(2))
    if not (0 <= hours <= 23):
        raise ValueError(f"Hour {hours} out of bounds (0-23).")
    if not (0 <= minutes <= 59):
        raise ValueError(f"Minute {minutes} out of bounds (0-59).")
    if minutes % 15 != 0:
        raise ValueError(f"Time '{time_str}' does not snap to 15-minute increments (must be :00, :15, :30, or :45).")
    return hours * 60 + minutes


def minutes_to_hhmm(total_minutes: int) -> str:
    hours = (total_minutes // 60) % 24
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def read_yaml_preserving_comments(path: Path) -> tuple[YAML, Any]:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f)
    return yaml, data


def write_yaml_atomically(path: Path, yaml: YAML, data: Any):
    # Save .bak copy first
    bak_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak_path)

    temp_file = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        os.replace(temp_file, path)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass


@router.get("")
def get_schedule():
    """
    GET /api/schedule:
    Returns videos_per_day, slots (HH:MM sorted), timezone,
    and for tomorrow each slot's assigned clip and status.
    """
    # 1. Read agent config
    yaml, agent_cfg = read_yaml_preserving_comments(AGENT_CONFIG_PATH)
    tz_name = agent_cfg.get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)

    raw_peaks = agent_cfg.get("peak_hours", [12, 14, 17, 19, 20, 21])
    parsed_slots = []
    for p in raw_peaks:
        try:
            if isinstance(p, int):
                parsed_slots.append(f"{p:02d}:00")
            elif isinstance(p, str):
                if ":" in p:
                    parts = p.split(":")
                    parsed_slots.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
                else:
                    parsed_slots.append(f"{int(p):02d}:00")
        except Exception:
            pass

    # Sort slots chronologically
    sorted_slots = sorted(set(parsed_slots), key=lambda x: parse_hhmm_to_minutes(x))

    target_count = int(agent_cfg.get("target_count", len(sorted_slots) or 6))

    # Read tomorrow's assigned clips from schedule joined with uploads
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    tomorrow_slots = []
    with get_ro_connection() as conn:
        try:
            cur = conn.execute("""
                SELECT s.slot_time, s.slot_hour, s.post_id, s.title, s.uploader,
                       COALESCE(u.status, s.status, 'queued') as status,
                       u.video_id, u.publish_at
                FROM schedule s
                LEFT JOIN uploads u ON s.post_id = u.post_id AND s.target_date = u.target_date
                WHERE s.target_date = ?
                ORDER BY s.slot_time ASC
            """, (tomorrow_str,))
            for r in cur.fetchall():
                d = dict(r)
                # Parse slot time string to HH:MM ET
                slot_iso = d.get("slot_time") or ""
                time_disp = "10:30 PM IST"
                slot_hhmm = "12:00"
                try:
                    dt = datetime.fromisoformat(slot_iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
                    dt_ny = dt.astimezone(ZoneInfo("America/New_York"))
                    slot_hhmm = f"{dt_ny.hour:02d}:{dt_ny.minute:02d}"
                    dt_ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
                    if dt_ist.minute == 0:
                        time_disp = f"{dt_ist.strftime('%I').lstrip('0')} {dt_ist.strftime('%p')} IST"
                    else:
                        time_disp = f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
                except Exception:
                    h = d.get("slot_hour", 12)
                    slot_hhmm = f"{h:02d}:00"
                    try:
                        dt_et = datetime(tomorrow_dt.year, tomorrow_dt.month, tomorrow_dt.day, h, 0, 0, tzinfo=ZoneInfo("America/New_York"))
                        dt_ist = dt_et.astimezone(ZoneInfo("Asia/Kolkata"))
                        if dt_ist.minute == 0:
                            time_disp = f"{dt_ist.strftime('%I').lstrip('0')} {dt_ist.strftime('%p')} IST"
                        else:
                            time_disp = f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
                    except Exception:
                        period = "PM" if h >= 12 else "AM"
                        h12 = h % 12 or 12
                        time_disp = f"{h12} {period} IST"
                d["slot_hhmm"] = slot_hhmm
                d["time_display"] = time_disp
                d["thumb_url"] = f"/api/thumbs/{d['post_id']}.jpg"
                tomorrow_slots.append(d)
        except Exception:
            pass

    return {
        "videos_per_day": target_count,
        "slots": sorted_slots,
        "timezone": tz_name,
        "display_timezone": "Asia/Kolkata",
        "tomorrow_date": tomorrow_str,
        "tomorrow_day_name": tomorrow_dt.strftime("%A"),
        "tomorrow_slots": tomorrow_slots,
        "can_retime_live": has_manage_scope(),
    }


@router.put("")
async def update_schedule(request: Request, body: ScheduleUpdateRequest):
    """
    PUT /api/schedule:
    Validate 1 to 12 slots, unique times, 15-minute steps, slot count == videos_per_day.
    Atomically updates agent/config.yaml and uploader/config.yaml with ruamel.yaml (.bak backup).
    Retimes scheduled YouTube clips if manage scope is available.
    """
    # Security Rule: Require Custom Header, JSON content-type, and Host check
    from dashboard.backend.security import verify_request_host
    verify_request_host(request)

    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported Media Type: Content-Type must be application/json.",
        )

    custom_header = request.headers.get("x-dashboard-request") or request.headers.get("x-requested-with")
    if not custom_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing security header: X-Dashboard-Request or X-Requested-With required.",
        )

    # Validation: 1 to 12 slots
    videos_per_day = body.videos_per_day
    if not (1 <= videos_per_day <= 12):
        raise HTTPException(status_code=400, detail="videos_per_day must be between 1 and 12.")

    slots_raw = body.slots
    if len(slots_raw) != videos_per_day:
        raise HTTPException(
            status_code=400,
            detail=f"Slot count ({len(slots_raw)}) does not match videos_per_day ({videos_per_day}).",
        )

    # Validate each slot format and 15-min increment
    minutes_list = []
    seen_slots = set()
    for s in slots_raw:
        try:
            mins = parse_hhmm_to_minutes(s)
            formatted = minutes_to_hhmm(mins)
            if formatted in seen_slots:
                raise HTTPException(status_code=400, detail=f"Duplicate slot time: {formatted}.")
            seen_slots.add(formatted)
            minutes_list.append(mins)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

    # Sort slots chronologically
    minutes_list.sort()
    sorted_slots = [minutes_to_hhmm(m) for m in minutes_list]

    # Read current agent config to inspect existing values and preserve comments
    agent_yaml, agent_cfg = read_yaml_preserving_comments(AGENT_CONFIG_PATH)
    tz_name = agent_cfg.get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)
    now_ny = datetime.now(tz)
    tomorrow_dt = now_ny + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
    tomorrow_day_name = tomorrow_dt.strftime("%A")

    current_target_count = int(agent_cfg.get("target_count", 0))
    raw_peaks = agent_cfg.get("peak_hours", [])
    current_slots = []
    for p in raw_peaks:
        try:
            if isinstance(p, int):
                current_slots.append(f"{p:02d}:00")
            elif isinstance(p, str):
                if ":" in p:
                    parts = p.split(":")
                    current_slots.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
                else:
                    current_slots.append(f"{int(p):02d}:00")
        except Exception:
            pass
    current_slots = sorted(set(current_slots), key=lambda x: parse_hhmm_to_minutes(x))

    # A no-change save must do nothing: no YouTube calls and no config rewrite
    if videos_per_day == current_target_count and sorted_slots == current_slots:
        return {
            "status": "success",
            "videos_per_day": videos_per_day,
            "slots": sorted_slots,
            "retimed_count": 0,
            "can_retime": has_manage_scope(),
            "notice": "No changes to schedule.",
        }

    # Query tomorrow's scheduled uploads directly from uploads table (deduplicated)
    scheduled_clips = []
    with get_ro_connection() as conn:
        try:
            cur = conn.execute("""
                SELECT DISTINCT u.post_id, u.video_id, u.slot_time, u.publish_at
                FROM uploads u
                WHERE u.target_date = ? AND u.status = 'scheduled' AND u.video_id IS NOT NULL
                ORDER BY u.slot_time ASC
            """, (tomorrow_str,))
            scheduled_clips = [dict(r) for r in cur.fetchall()]
        except Exception:
            scheduled_clips = []

    # 1. Parse each scheduled clip's current uploaded slot time in ET
    parsed_clips = []
    for clip in scheduled_clips:
        raw_time = clip.get("slot_time") or ""
        dt_et = None
        try:
            if raw_time:
                dt_et = datetime.fromisoformat(raw_time).astimezone(tz)
            elif clip.get("publish_at"):
                dt_et = datetime.fromisoformat(clip["publish_at"].replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            pass

        if dt_et:
            clip["dt_et"] = dt_et
            clip["uploaded_hhmm"] = f"{dt_et.hour:02d}:{dt_et.minute:02d}"
            parsed_clips.append(clip)

    # Sort parsed clips chronologically by their uploaded slot time
    parsed_clips.sort(key=lambda c: parse_hhmm_to_minutes(c["uploaded_hhmm"]))

    # 2. Leave every clip whose slot time is unchanged
    available_new_slots = list(sorted_slots)
    unchanged_clips = []
    leftover_clips = []

    for clip in parsed_clips:
        slot_hhmm = clip["uploaded_hhmm"]
        if slot_hhmm in available_new_slots:
            available_new_slots.remove(slot_hhmm)
            unchanged_clips.append(clip)
        else:
            leftover_clips.append(clip)

    # 3. Match only the leftover old times to the leftover new times in order
    clips_to_retime = []
    for clip, target_hhmm in zip(leftover_clips, available_new_slots):
        th, tm = int(target_hhmm.split(":")[0]), int(target_hhmm.split(":")[1])
        new_dt = tomorrow_dt.replace(hour=th, minute=tm, second=0, microsecond=0)
        if new_dt <= now_ny:
            continue

        new_dt_utc = new_dt.astimezone(timezone.utc)
        new_publish_at_rfc3339 = new_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        new_slot_time_iso = new_dt.isoformat()

        clips_to_retime.append({
            "post_id": clip["post_id"],
            "video_id": clip["video_id"],
            "old_slot_hour": clip["dt_et"].hour,
            "old_slot_time": clip.get("slot_time"),
            "old_publish_at": clip.get("publish_at"),
            "slot_hour": th,
            "slot_time": new_slot_time_iso,
            "publish_at": new_publish_at_rfc3339,
            "target_slot_hhmm": target_hhmm,
        })

    can_retime = has_manage_scope()
    retimed_count = 0

    if can_retime and clips_to_retime:
        # Step 1: Local write in transaction first
        backend_db.update_local_retimed_data(
            target_date=tomorrow_str,
            retimed_items=clips_to_retime,
            db_path=backend_db.DB_PATH,
            data_dir=SCHEDULE_DATA_DIR,
        )

        # Step 2: Call YouTube API for moved clips
        youtube_updated = []
        youtube_failed = False
        for item in clips_to_retime:
            ok = update_video_publish_time(item["video_id"], item["publish_at"])
            if ok:
                youtube_updated.append(item)
            else:
                youtube_failed = True
                break

        # Step 3: Roll back everything if any YouTube call fails
        if youtube_failed:
            for item in youtube_updated:
                update_video_publish_time(item["video_id"], item["old_publish_at"])
            backend_db.rollback_local_retimed_data(
                target_date=tomorrow_str,
                retimed_items=clips_to_retime,
                db_path=backend_db.DB_PATH,
                data_dir=SCHEDULE_DATA_DIR,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update publish time on YouTube. All local and remote changes were safely rolled back.",
            )

        retimed_count = len(clips_to_retime)
        notice = f"{tomorrow_day_name}’s {retimed_count} moved clips were retimed on YouTube and saved."
    elif not can_retime and clips_to_retime:
        notice = "New times saved for future runs only. To retime existing scheduled clips on YouTube, run: python upload.py auth --manage"
    else:
        notice = f"Schedule saved! {videos_per_day} slots will take effect on the next daily run."

    # Step 4: Write updated configs with ruamel.yaml preserving comments atomically
    agent_cfg["target_count"] = videos_per_day
    # Update peak_hours list in-place (clear it and fill it again) so its comments stay attached
    if "peak_hours" in agent_cfg and hasattr(agent_cfg["peak_hours"], "clear"):
        seq = agent_cfg["peak_hours"]
        existing_comment = None
        if hasattr(seq, "ca") and hasattr(seq.ca, "items") and seq.ca.items:
            for k in sorted(seq.ca.items.keys()):
                if seq.ca.items[k] and any(tok for tok in seq.ca.items[k] if tok):
                    existing_comment = seq.ca.items[k]
                    break

        seq.clear()
        for s in sorted_slots:
            seq.append(s)

        if hasattr(seq, "ca") and hasattr(seq.ca, "items"):
            if -1 in seq.ca.items and len(seq) > 0:
                seq.ca.items[len(seq) - 1] = seq.ca.items.pop(-1)
            elif existing_comment and len(seq) > 0:
                seq.ca.items[len(seq) - 1] = existing_comment
    else:
        agent_cfg["peak_hours"] = sorted_slots
    write_yaml_atomically(AGENT_CONFIG_PATH, agent_yaml, agent_cfg)

    uploader_yaml, uploader_cfg = read_yaml_preserving_comments(UPLOADER_CONFIG_PATH)
    if "upload" not in uploader_cfg:
        uploader_cfg["upload"] = {}
    uploader_cfg["upload"]["max_uploads_per_day"] = videos_per_day
    write_yaml_atomically(UPLOADER_CONFIG_PATH, uploader_yaml, uploader_cfg)

    return {
        "status": "success",
        "videos_per_day": videos_per_day,
        "slots": sorted_slots,
        "retimed_count": retimed_count,
        "can_retime": can_retime,
        "notice": notice,
    }
