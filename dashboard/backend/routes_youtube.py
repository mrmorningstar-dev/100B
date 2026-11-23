import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Request, HTTPException, Response, status
from fastapi.responses import FileResponse, StreamingResponse

from dashboard.backend.db import get_ro_connection, WORKSPACE_DIR, dashboard_today
from dashboard.backend.thumbs import resolve_video_path_for_post, get_or_generate_thumbnail, get_placeholder_thumbnail
from dashboard.backend.youtube_api import fetch_live_video_stats

router = APIRouter(prefix="/api", tags=["youtube"])


def resolve_date(date_param: str, tz_name: str = "America/New_York") -> str:
    """Resolve display date parameter relative to Asia/Kolkata dashboard date."""
    today_ist = dashboard_today()
    s = (date_param or "today").strip().lower()
    if s in ("today", "now"):
        return today_ist
    elif s in ("yesterday", "prev", "prev_day"):
        t_dt = datetime.strptime(today_ist, "%Y-%m-%d")
        return (t_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    elif s in ("tomorrow", "next_day"):
        t_dt = datetime.strptime(today_ist, "%Y-%m-%d")
        return (t_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return today_ist


def resolve_publish_time(row: Dict[str, Any], live_info: Dict[str, Any]) -> tuple[Optional[datetime], str]:
    """
    Resolve the publication or broadcast time (publishAt or slot_time, falling back to uploaded_at).
    Scheduled batch uploads are pushed to YouTube the evening before, so uploaded_at reflects the
    batch render/upload time, while publishAt (or slot_time) reflects the actual broadcast time.
    Returns: (target_datetime_utc, formatted_et_str)
    """
    yt_status = live_info.get("status", {})
    publish_at_str = yt_status.get("publishAt") or row.get("publish_at")
    slot_time_str = row.get("slot_time")
    uploaded_at_str = row.get("uploaded_at")

    target_dt: Optional[datetime] = None

    # Priority 1: publishAt (from YouTube API status or database)
    if publish_at_str:
        try:
            dt = datetime.fromisoformat(publish_at_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            target_dt = dt
        except Exception:
            pass

    # Priority 2: slot_time (from schedule / uploads table)
    if target_dt is None and slot_time_str:
        try:
            dt = datetime.fromisoformat(slot_time_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
            target_dt = dt.astimezone(timezone.utc)
        except Exception:
            pass

    # Priority 3: uploaded_at (fallback for immediate unscheduled uploads)
    if target_dt is None and uploaded_at_str:
        try:
            dt = datetime.fromisoformat(uploaded_at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            target_dt = dt
        except Exception:
            pass

    if target_dt is None:
        return None, "recently"

    ist_dt = target_dt.astimezone(ZoneInfo("Asia/Kolkata"))
    if ist_dt.minute == 0:
        time_text = f"{ist_dt.strftime('%I').lstrip('0')} {ist_dt.strftime('%p')} IST"
    else:
        time_text = f"{ist_dt.strftime('%I:%M %p').lstrip('0')} IST"

    return target_dt, time_text


def determine_clip_status(row: Dict[str, Any], live_info: Dict[str, Any], now_utc: datetime) -> tuple[str, str]:
    """
    Status rules:
      - Published (public, publish time passed)
      - On air (published within the last 60 minutes)
      - Scheduled (private with a future publishAt / slot_time)
      - Draft (private, no publishAt)
      - Failed (status='failed' or error)
    Uses publishAt or slot_time for broadcast timing rather than upload time.
    Returns: (status_label, status_detail_text)
    """
    db_status = (row.get("status") or "").lower()
    last_error = row.get("last_error")

    if db_status in ("failed", "error") or last_error:
        return "Failed", f"Upload failed: {last_error or 'Unknown error'}"

    # If the video has a video_id but YouTube does not return it in videos.list
    vid_id = row.get("video_id")
    if vid_id and live_info.get("missing"):
        return "Missing on YouTube", "Missing on YouTube (deleted or removed)"

    yt_status = live_info.get("status", {})
    yt_privacy = yt_status.get("privacyStatus")
    yt_publish_at = yt_status.get("publishAt") or row.get("publish_at")
    yt_stats = live_info.get("statistics", {})

    views_str = f"{int(yt_stats['viewCount']):,}" if "viewCount" in yt_stats else "—"
    likes_str = f"{int(yt_stats['likeCount']):,}" if "likeCount" in yt_stats else "—"

    target_dt, time_text = resolve_publish_time(row, live_info)

    # 0. Local Queued (not yet uploaded to YouTube)
    if not vid_id and not row.get("uploaded_at") and db_status not in ("uploaded", "published"):
        return "Queued", f"Queued locally · Slot {time_text}"

    # 1. Scheduled (future publishAt / slot_time on YouTube)
    if target_dt and target_dt > now_utc:
        if yt_privacy == "private" or yt_publish_at or db_status in ("scheduled", "queued", "uploaded"):
            return "Scheduled", f"Goes live at {time_text}"

    # 2. Published / On air (publish time passed OR public on YouTube)
    if yt_privacy == "public" or db_status in ("published", "uploaded") or (target_dt and target_dt <= now_utc and db_status != "draft"):
        is_on_air = False
        if target_dt:
            diff_minutes = (now_utc - target_dt).total_seconds() / 60.0
            if 0 <= diff_minutes <= 60:
                is_on_air = True

        if is_on_air:
            return "On air", f"On air since {time_text} · {views_str} views"
        return "Published", f"Published {time_text} · {views_str} views · {likes_str} likes"

    # 3. Draft
    if yt_privacy == "private" or db_status in ("draft", "queued"):
        return "Draft", "Private draft"

    return "Draft", "Queued"


@router.get("/youtube/day/{date}")
def get_youtube_day(date: str):
    """
    GET /api/youtube/day/{date} (today, yesterday, or YYYY-MM-DD):
    Rundown of videos published, on air, or scheduled for date, with live stats.
    """
    tz_name = "America/New_York"
    tz = ZoneInfo(tz_name)
    target_date = resolve_date(date, tz_name)
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)

    rows = []
    with get_ro_connection() as conn:
        try:
            cur = conn.execute("""
                SELECT s.slot_time, s.slot_hour, s.post_id, s.title, s.uploader, s.source_url, s.video_path,
                       COALESCE(u.status, s.status, 'queued') as status,
                       u.video_id, u.uploaded_at, u.publish_at, u.privacy, u.last_error,
                       COALESCE(u.claim_status, 'none') as claim_status,
                       COALESCE(u.rights_status, s.rights_status, 'unreviewed') as rights_status,
                       COALESCE(u.rights_risk, s.rights_risk, 0) as rights_risk
                FROM schedule s
                LEFT JOIN uploads u ON s.post_id = u.post_id AND s.target_date = u.target_date
                WHERE s.target_date = ?
                ORDER BY s.slot_time ASC
            """, (target_date,))
            rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            pass

        # If schedule had no rows for target_date, check uploads table alone
        if not rows:
            try:
                cur = conn.execute("""
                    SELECT u.slot_time, u.post_id, u.status, u.video_id, u.uploaded_at,
                           u.publish_at, u.privacy, u.last_error,
                           r.title, r.uploader
                    FROM uploads u
                    LEFT JOIN reserve_pool r ON u.post_id = r.post_id
                    WHERE u.target_date = ?
                    ORDER BY u.slot_time ASC
                """, (target_date,))
                rows = [dict(r) for r in cur.fetchall()]
            except Exception:
                pass

    video_ids = [r["video_id"] for r in rows if r.get("video_id")]
    live_stats = fetch_live_video_stats(video_ids)

    # Process items
    published_count = 0
    on_air_count = 0
    scheduled_count = 0
    total_views = 0

    items = []
    for r in rows:
        vid_id = r.get("video_id")
        live = live_stats.get(vid_id, {})
        status_label, status_detail = determine_clip_status(r, live, now_utc)

        if status_label == "Published":
            published_count += 1
        elif status_label == "On air":
            on_air_count += 1
        elif status_label == "Scheduled":
            scheduled_count += 1

        stats_obj = live.get("statistics", {})
        if "viewCount" in stats_obj:
            try:
                total_views += int(stats_obj["viewCount"])
            except Exception:
                pass

        slot_time_iso = r.get("slot_time") or ""
        time_disp = "10:30 PM IST"
        try:
            dt = datetime.fromisoformat(slot_time_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
            dt_ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
            if dt_ist.minute == 0:
                time_disp = f"{dt_ist.strftime('%I').lstrip('0')} {dt_ist.strftime('%p')} IST"
            else:
                time_disp = f"{dt_ist.strftime('%I:%M %p').lstrip('0')} IST"
        except Exception:
            h = r.get("slot_hour", 12)
            try:
                d_obj = datetime.strptime(target_date, "%Y-%m-%d")
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

        items.append({
            "post_id": r["post_id"],
            "video_id": vid_id,
            "title": r.get("title") or "Comedy Clip",
            "uploader": r.get("uploader") or "unknown",
            "slot_time": slot_time_iso,
            "time_display": time_disp,
            "status": status_label,
            "status_detail": status_detail,
            "claim_status": r.get("claim_status") or "none",
            "rights_status": r.get("rights_status") or "unreviewed",
            "rights_risk": r.get("rights_risk") or 0,
            "views": stats_obj.get("viewCount"),
            "likes": stats_obj.get("likeCount"),
            "youtube_url": f"https://www.youtube.com/shorts/{vid_id}" if vid_id else None,
            "studio_url": f"https://studio.youtube.com/video/{vid_id}/edit" if vid_id else None,
            "thumb_url": f"/api/thumbs/{r['post_id']}.jpg",
            "stream_url": f"/api/video/{r['post_id']}/stream",
            "has_local_file": resolve_video_path_for_post(r["post_id"]) is not None,
        })

    day_header = target_dt.strftime("%A %d %B")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    is_late_broadcast_window = (now_ist.hour < 7 or (now_ist.hour == 7 and now_ist.minute <= 30))

    return {
        "date": target_date,
        "display_timezone": "Asia/Kolkata",
        "day_header": day_header,
        "is_late_broadcast_window": is_late_broadcast_window,
        "published_count": published_count,
        "on_air_count": on_air_count,
        "scheduled_count": scheduled_count,
        "total_views": total_views,
        "clips": items,
    }


@router.get("/thumbs/{post_id}.jpg")
def get_thumbnail(post_id: str):
    """Serve cached 1-second thumbnail, or generate it via ffmpeg; fall back to placeholder with 200 OK."""
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", post_id)
    thumb_path = get_or_generate_thumbnail(clean_id)
    if thumb_path and thumb_path.is_file():
        return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
    placeholder_path = get_placeholder_thumbnail()
    return FileResponse(placeholder_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})


@router.get("/video/{post_id}/stream")
def stream_video(post_id: str, request: Request):
    """
    HTTP Range streaming of local video file for post_id.
    Never accepts arbitrary path from client; validates post_id against database.
    Clamps range end to file_size - 1 if request exceeds file bounds.
    """
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", post_id)
    video_path = resolve_video_path_for_post(clean_id)

    if not video_path or not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found for this post_id.")

    file_size = video_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # Parse range header: e.g. "bytes=0-1024" or "bytes=1024-"
        range_match = re.match(r"^bytes=(\d+)-(\d*)$", range_header.strip())
        if range_match:
            start = int(range_match.group(1))
            end_str = range_match.group(2)
            end = int(end_str) if end_str else file_size - 1

            # Clamp end to file_size - 1 if it exceeds file bounds
            if end >= file_size:
                end = file_size - 1

            if start >= file_size or start > end:
                return Response(
                    status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                    headers={"Content-Range": f"bytes */{file_size}"},
                )

            chunk_length = (end - start) + 1

            def range_generator(path: Path, start_offset: int, length: int):
                with open(path, "rb") as f:
                    f.seek(start_offset)
                    remaining = length
                    chunk_size = 1024 * 64
                    while remaining > 0:
                        read_bytes = min(remaining, chunk_size)
                        data = f.read(read_bytes)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_length),
                "Content-Type": "video/mp4",
            }
            return StreamingResponse(
                range_generator(video_path, start, chunk_length),
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                headers=headers,
            )

    # Full response when Range header is not present
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": "video/mp4",
    }
    return FileResponse(video_path, media_type="video/mp4", headers=headers)
