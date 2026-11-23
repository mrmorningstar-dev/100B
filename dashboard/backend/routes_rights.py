import os
import json
import sqlite3
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Header, Body, Request, status, Depends
from pydantic import BaseModel

import sys
from dashboard.backend import db as backend_db
from dashboard.backend.db import WORKSPACE_DIR

def get_ro_connection():
    return backend_db.get_ro_connection()

def get_rw_connection():
    return backend_db.get_rw_connection()

_root_dir = str(WORKSPACE_DIR)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)
_agent_dir = str(WORKSPACE_DIR / "agent")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:
    from core.rights.youtube_gate import YouTubeRightsGate
    from core.rights.perceptual import HistoryClaimsChecker
except ImportError:
    from agent.core.rights.youtube_gate import YouTubeRightsGate
    from agent.core.rights.perceptual import HistoryClaimsChecker

from claims_sync.config import load_claims_config, set_dry_run
from claims_sync.parser import parse_studio_paste
from claims_sync.actions import undo_auto_hold, count_today_auto_actions
from claims_sync.auth import is_gmail_authenticated

router = APIRouter(prefix="/api/rights", tags=["rights"])
CONFIG_PATH = WORKSPACE_DIR / "agent" / "config.yaml"


def load_rights_config() -> Dict[str, Any]:
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return data.get("rights", {})
        except Exception:
            pass
    return {}


def verify_rights_write_security(request: Request):
    """
    Security Rule: Require Custom Header, JSON content-type, and Host check.
    Matches the guard on the Schedule save route.
    """
    host = request.headers.get("host", "").lower()
    if not (host.startswith("127.0.0.1") or host.startswith("localhost") or host.startswith("testserver")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Host check failed. Requests must originate from localhost/127.0.0.1.",
        )

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


class MarkClearRequest(BaseModel):
    post_id: str


class MarkClaimedRequest(BaseModel):
    post_id: str
    video_id: Optional[str] = None
    claim_type: str = "audio"  # audio, visual, both
    claimant: str
    asset_title: Optional[str] = None
    found_in: Optional[str] = None
    action: str = "held"  # kept, held, pulled, disputed
    notes: Optional[str] = None


class HoldRequest(BaseModel):
    post_id: str
    video_id: Optional[str] = None


class ReplaceFromReserveRequest(BaseModel):
    post_id: str
    action_on_claimed: str = "held"  # held, delete
    delete_confirmation: Optional[str] = None


class PublishedActionRequest(BaseModel):
    post_id: str
    video_id: Optional[str] = None
    action: str  # keep, make_private, delete, remove_record
    delete_confirmation: Optional[str] = None


class AddBlocklistRequest(BaseModel):
    type: str  # text, domain, uploader, subreddit, claimant, track
    value: str


class ToggleDryRunRequest(BaseModel):
    dry_run: bool


class ParseStudioPasteRequest(BaseModel):
    text: str


class SaveManualClaimRequest(BaseModel):
    post_id: str
    video_id: Optional[str] = None
    asset_title: Optional[str] = None
    track_artist: Optional[str] = None
    claimant: Optional[str] = None
    claim_type: str = "audio"
    affects_reach: bool = False
    notes: Optional[str] = None


class MarkClaimReleasedRequest(BaseModel):
    claim_id: Optional[int] = None
    post_id: Optional[str] = None


class UndoAutoHoldRequest(BaseModel):
    post_id: str


@router.get("/status")
def get_rights_status():
    """
    Returns active layers status, provider quotas used today against caps,
    local-only warning, and aggregate claim metrics.
    """
    cfg = load_rights_config()
    is_local_only = cfg.get("local_only", True)
    shazam_cap = cfg.get("providers", {}).get("shazam", {}).get("daily_cap", 100)
    vision_cap = cfg.get("providers", {}).get("google_vision", {}).get("daily_cap", 25)

    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    with get_ro_connection() as conn:
        # Provider usage
        cur = conn.execute("SELECT provider, calls, errors FROM provider_usage WHERE day = ?", (today_str,))
        usage_rows = {r["provider"]: dict(r) for r in cur.fetchall()}

        # Total claims and real published clips
        total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q_pub = """
            SELECT COUNT(*) FROM uploads u
            WHERE u.video_id IS NOT NULL
              AND u.status != 'deleted'
              AND (
                u.status IN ('uploaded', 'published')
                OR (u.publish_at IS NOT NULL AND u.publish_at <= ?)
                OR (u.publish_at IS NULL AND u.target_date <= ?)
              )
        """
        total_published = conn.execute(q_pub, (now_utc_str, today_str)).fetchone()[0]
        claim_rate = round((total_claims / total_published) * 100, 2) if total_published > 0 else 0.0

        # Subreddit claim breakdown
        cur_sub = conn.execute("""
            SELECT source_name, COUNT(*) as cnt
            FROM claims
            WHERE source_name IS NOT NULL
            GROUP BY source_name
        """)
        sub_rates = {r["source_name"]: r["cnt"] for r in cur_sub.fetchall()}

    import importlib.util
    shazam_ready = importlib.util.find_spec("shazamio") is not None  # free, no API key needed
    has_vision_key = bool(os.environ.get("GOOGLE_VISION_API_KEY"))

    shazam_calls = usage_rows.get("shazam", {}).get("calls", 0)
    shazam_errors = usage_rows.get("shazam", {}).get("errors", 0)
    vision_calls = usage_rows.get("google_vision", {}).get("calls", 0)
    vision_errors = usage_rows.get("google_vision", {}).get("errors", 0)

    # Flag an error only when most of today's requests failed (one network blip is not an outage)
    is_shazam_failing = bool(shazam_errors > 0 and shazam_errors * 2 >= max(shazam_calls, 1))

    active_layers = {
        "layer1_agency_ocr": True,
        "layer2a_local_music": True,
        "layer2b_fingerprint": shazam_ready and not is_local_only and not is_shazam_failing,
        "layer3_cloud_vision": has_vision_key and not is_local_only,
        "layer4_scheduled_gate": True,
        "layer5_history_learned": True,
    }

    return {
        "is_local_only": is_local_only,
        "unreviewed_policy": cfg.get("unreviewed_policy", "hold"),
        "active_layers": active_layers,
        "providers": {
            "shazam": {
                "configured": shazam_ready,
                "calls_today": shazam_calls,
                "errors_today": shazam_errors,
                "daily_cap": shazam_cap,
                "status": "error" if is_shazam_failing else ("active" if shazam_ready and not is_local_only else "idle"),
                "error_message": "Shazam: most recent requests failed" if is_shazam_failing else None,
            },
            "google_vision": {
                "configured": has_vision_key,
                "calls_today": vision_calls,
                "errors_today": vision_errors,
                "daily_cap": vision_cap,
                "monthly_free_tier": 1000,
            },
        },
        "stats": {
            "total_claims": total_claims,
            "total_published": total_published,
            "claims_per_100_published": claim_rate,
            "claims_by_subreddit": sub_rates,
        },
    }


@router.get("/review")
def get_needs_review_queue():
    """
    Returns every scheduled clip that is not public yet (going live today, tomorrow, or upcoming),
    with risk badges, per-layer findings, and action capabilities (Mark Clear, Hold, Mark Claimed, Replace).
    """
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_str = now_ny.strftime("%Y-%m-%d")

    with get_ro_connection() as conn:
        cur = conn.execute("""
            SELECT s.target_date, s.slot_time, s.slot_hour, s.post_id, s.title, s.uploader, s.source_url, s.video_path,
                   COALESCE(u.rights_status, s.rights_status, 'unreviewed') as rights_status,
                   COALESCE(u.rights_risk, s.rights_risk, 0) as rights_risk,
                   u.video_id, u.publish_at, u.privacy, u.status as upload_status
            FROM schedule s
            LEFT JOIN uploads u ON s.post_id = u.post_id
            WHERE (s.status IS NULL OR s.status != 'deleted')
              AND (u.status IS NULL OR u.status != 'deleted')
              AND (s.target_date >= ? OR u.rights_status = 'held' OR u.privacy = 'private')
            ORDER BY s.target_date ASC, s.slot_time ASC
        """, (today_str,))
        rows = [dict(r) for r in cur.fetchall()]

    # Fetch live YouTube status (cached) to verify if already public or missing
    video_ids = [r["video_id"] for r in rows if r.get("video_id")]
    live_stats = {}
    if video_ids:
        try:
            from dashboard.backend.youtube_api import fetch_live_video_stats
            live_stats = fetch_live_video_stats(video_ids)
        except Exception:
            live_stats = {}

    filtered_rows = []
    for r in rows:
        vid = r.get("video_id")
        live = live_stats.get(vid, {}) if vid else {}

        # If video is found on YouTube:
        if vid and live.get("found"):
            yt_privacy = live.get("status", {}).get("privacyStatus")
            # If already public, it has gone live -> exclude from Needs Review
            if yt_privacy == "public":
                continue
            r["privacy"] = yt_privacy or r.get("privacy") or "private"
        elif vid and live.get("missing"):
            # If deleted on YouTube -> exclude from Needs Review
            continue
        else:
            # If no live status or not uploaded yet:
            if r.get("privacy") == "public":
                continue
            r["privacy"] = r.get("privacy") or "private"

        filtered_rows.append(r)

    # Attach rights_checks details for each clip
    with get_ro_connection() as conn:
        for r in filtered_rows:
            pid = r["post_id"]
            check_row = conn.execute("""
                SELECT layers_json, risk_score, decision, reasons_json
                FROM rights_checks
                WHERE post_id = ?
                ORDER BY id DESC LIMIT 1
            """, (pid,)).fetchone()

            if check_row:
                try:
                    r["layers"] = json.loads(check_row["layers_json"])
                except Exception:
                    r["layers"] = {}
                try:
                    r["reasons"] = json.loads(check_row["reasons_json"])
                except Exception:
                    r["reasons"] = []
                r["risk_score"] = check_row["risk_score"]
                r["decision"] = check_row["decision"]
                if r["rights_status"] == "unreviewed":
                    r["rights_status"] = "cleared" if check_row["decision"] == "pass" else "needs_check"
            else:
                r["layers"] = {}
                r["reasons"] = []
                r["risk_score"] = r.get("rights_risk") or 0
                r["decision"] = "pass" if r["risk_score"] < 30 else ("review" if r["risk_score"] < 80 else "block")
                if r["rights_status"] == "unreviewed":
                    r["rights_status"] = "cleared" if r["decision"] == "pass" else "needs_check"

            r["thumb_url"] = f"/api/youtube/thumb/{pid}"

            # Attach latest claim from claims table
            claim_row = conn.execute("""
                SELECT id, state, source, claim_type, claimant, asset_title, track_title, track_artist, affects_reach, detected_at, created_at, notes
                FROM claims
                WHERE post_id = ? OR (video_id = ? AND video_id IS NOT NULL AND video_id != '')
                ORDER BY id DESC LIMIT 1
            """, (pid, vid)).fetchone()

            if claim_row:
                r["claim"] = dict(claim_row)
                c_state = claim_row["state"]
                if c_state == "active":
                    r["claim_chip"] = "claimed"
                elif c_state == "released":
                    r["claim_chip"] = "released"
                elif c_state == "unknown_pending_confirmation":
                    r["claim_chip"] = "pending_confirmation"
                else:
                    r["claim_chip"] = "possible"
            else:
                r["claim"] = None
                if r.get("rights_risk", 0) >= 40 or r.get("decision") == "review":
                    r["claim_chip"] = "possible"
                else:
                    r["claim_chip"] = "none"

    target_date = filtered_rows[0]["target_date"] if filtered_rows else today_str
    return {"target_date": target_date, "clips": filtered_rows}


@router.get("/published")
def get_recently_published(days: int = 7):
    """
    Returns clips published within the last 7 days with action capabilities:
    Keep, Make private, and Delete (typed DELETE).
    Uses live YouTube status (cached) to resolve real privacy status and missing videos.
    """
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    since_date = (now_ny - timedelta(days=days)).strftime("%Y-%m-%d")

    with get_ro_connection() as conn:
        cur = conn.execute("""
            SELECT u.post_id, u.video_id, u.target_date, u.slot_time, u.publish_at, u.status,
                   u.privacy, u.claim_status, u.rights_status, u.rights_risk, s.title, s.uploader
            FROM uploads u
            LEFT JOIN schedule s ON u.post_id = s.post_id
            WHERE u.target_date >= ?
            ORDER BY u.target_date DESC, u.slot_time DESC
        """, (since_date,))
        rows = [dict(r) for r in cur.fetchall()]

        # Attach claim info from claims table if present
        cur_claims = conn.execute("""
            SELECT post_id, video_id, id, state, source, action, claimant, claim_type, asset_title, track_title, track_artist, affects_reach, detected_at, created_at, notes
            FROM claims ORDER BY id ASC
        """)
        claims_rows = [dict(c) for c in cur_claims.fetchall()]
        claims_map_by_post = {c["post_id"]: c for c in claims_rows if c.get("post_id")}
        claims_map_by_vid = {c["video_id"]: c for c in claims_rows if c.get("video_id")}

    video_ids = [r["video_id"] for r in rows if r.get("video_id")]
    live_stats = {}
    if video_ids:
        try:
            from dashboard.backend.youtube_api import fetch_live_video_stats
            live_stats = fetch_live_video_stats(video_ids)
        except Exception:
            live_stats = {}

    now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = now_ny.strftime("%Y-%m-%d")

    changed_privacy = []
    published_rows = []
    for r in rows:
        vid = r.get("video_id")
        live = live_stats.get(vid, {}) if vid else {}
        is_missing = bool(vid and live.get("missing"))
        yt_privacy = (live.get("status", {}).get("privacyStatus") if live.get("found") else None) or r.get("privacy") or "private"

        # Exclude upcoming scheduled private clips that have NOT gone live yet
        # (they belong in Needs Review, not in Recently Published!)
        is_future_scheduled = (
            not is_missing
            and yt_privacy != "public"
            and r.get("status") == "scheduled"
            and (
                r.get("target_date", "") > today_str
                or (r.get("target_date") == today_str and (r.get("publish_at") or "") > now_utc_str)
            )
        )
        if is_future_scheduled:
            continue

        r["thumb_url"] = f"/api/youtube/thumb/{r['post_id']}"
        claim_info = claims_map_by_post.get(r["post_id"]) or claims_map_by_vid.get(vid)
        if claim_info:
            r["claim"] = claim_info
            r["claim_action"] = claim_info.get("action")
            r["claimant"] = claim_info.get("claimant")
            r["claim_type"] = claim_info.get("claim_type")
            c_state = claim_info.get("state")
            if c_state == "active":
                r["claim_chip"] = "claimed"
            elif c_state == "released":
                r["claim_chip"] = "released"
            elif c_state == "unknown_pending_confirmation":
                r["claim_chip"] = "pending_confirmation"
            else:
                r["claim_chip"] = "possible"
        else:
            r["claim"] = None
            r["claim_action"] = None
            r["claimant"] = None
            r["claim_type"] = None
            r["claim_chip"] = "none"

        if vid and live.get("missing"):
            r["is_missing"] = True
            r["live_status"] = "missing"
            r["privacy"] = "missing"
            r["live_privacy"] = "missing"
            r["views"] = 0
            r["likes"] = 0
        elif vid and live.get("found"):
            r["is_missing"] = False
            if yt_privacy != r.get("privacy"):
                changed_privacy.append((yt_privacy, vid))
            r["privacy"] = yt_privacy
            r["live_privacy"] = yt_privacy
            r["live_status"] = "published" if yt_privacy == "public" else "held"
            stats = live.get("statistics", {})
            r["views"] = int(stats.get("viewCount", 0)) if "viewCount" in stats else 0
            r["likes"] = int(stats.get("likeCount", 0)) if "likeCount" in stats else 0
        else:
            is_del = r.get("status") == "deleted"
            r["is_missing"] = is_del
            r["live_status"] = "missing" if is_del else r.get("status")
            r["live_privacy"] = "missing" if is_del else (r.get("privacy") or "private")
            r["views"] = 0
            r["likes"] = 0

        published_rows.append(r)

    # Sync any updated live privacy back to DB
    if changed_privacy:
        try:
            with get_rw_connection() as conn:
                for yt_priv, vid in changed_privacy:
                    conn.execute("UPDATE uploads SET privacy = ? WHERE video_id = ?", (yt_priv, vid))
        except Exception:
            pass

    return published_rows


@router.post("/mark-clear", dependencies=[Depends(verify_rights_write_security)])
def mark_clip_clear(req: MarkClearRequest):
    """
    Marks a scheduled clip cleared. Restores scheduled publishAt or assigns next free slot.
    """
    gate = YouTubeRightsGate(db_path=backend_db.DB_PATH)
    # Try creating YouTube service if tokens exist
    yt_service = None
    try:
        from dashboard.backend.youtube_api import get_youtube_service
        yt_service = get_youtube_service()
    except Exception:
        pass

    res = gate.mark_clear(req.post_id, youtube_service=yt_service)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to mark cleared"))
    return res


@router.post("/mark-claimed", dependencies=[Depends(verify_rights_write_security)])
def mark_clip_claimed(req: MarkClaimedRequest):
    """
    Registers a Content ID claim against a video, auto-extracts blocklist suggestions,
    and updates clip status.
    """
    with get_rw_connection() as conn:
        # Check source name and uploader from schedule
        row = conn.execute("""
            SELECT s.source_url, s.uploader
            FROM schedule s
            WHERE s.post_id = ?
        """, (req.post_id,)).fetchone()

        uploader = row["uploader"] if row and row["uploader"] else "Unknown"
        s_url = row["source_url"] if row and row["source_url"] else ""
        source_name = "r/funny"
        if s_url and "reddit.com/r/" in s_url:
            try:
                source_name = f"r/{s_url.split('/r/')[1].split('/')[0]}"
            except Exception:
                pass

        cur = conn.execute("""
            INSERT INTO claims (
                video_id, post_id, claim_type, claimant, asset_title,
                found_in, action, noted_at, source_name, uploader, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req.video_id,
            req.post_id,
            req.claim_type,
            req.claimant,
            req.asset_title,
            req.found_in,
            req.action,
            datetime.now().isoformat(),
            source_name,
            uploader,
            req.notes,
        ))
        claim_id = cur.lastrowid

        # Update uploads & schedule
        conn.execute("UPDATE uploads SET claim_status = 'claimed', rights_status = 'claimed' WHERE post_id = ?", (req.post_id,))
        conn.execute("UPDATE schedule SET rights_status = 'claimed' WHERE post_id = ?", (req.post_id,))

        # Extract and insert suggested blocklist additions
        checker = HistoryClaimsChecker()
        claim_dict = {
            "id": claim_id,
            "claimant": req.claimant,
            "asset_title": req.asset_title,
            "claim_type": req.claim_type,
            "source_name": source_name,
            "uploader": uploader,
        }
        suggestions = checker.generate_claim_suggestions(claim_dict)
        for s in suggestions:
            conn.execute("""
                INSERT OR IGNORE INTO rights_blocklist (type, value, source_claim_id, status)
                VALUES (?, ?, ?, 'pending_confirmation')
            """, (s["type"], s["value"], claim_id))

    return {
        "success": True,
        "claim_id": claim_id,
        "post_id": req.post_id,
        "suggestions_created": len(suggestions),
        "suggestions": suggestions,
    }


@router.post("/hold", dependencies=[Depends(verify_rights_write_security)])
def hold_clip(req: HoldRequest):
    """Sets video to private on YouTube and removes publishAt."""
    gate = YouTubeRightsGate(db_path=backend_db.DB_PATH)
    yt_service = None
    try:
        from dashboard.backend.youtube_api import get_youtube_service
        yt_service = get_youtube_service()
    except Exception:
        pass

    res = gate.mark_held(req.post_id, youtube_service=yt_service)
    return res


@router.post("/replace-from-reserve", dependencies=[Depends(verify_rights_write_security)])
def replace_claimed_clip_from_reserve(req: ReplaceFromReserveRequest):
    """
    Safe transactional pull-and-replace from reserve pool.
    """
    gate = YouTubeRightsGate(db_path=backend_db.DB_PATH)
    yt_service = None
    try:
        from dashboard.backend.youtube_api import get_youtube_service
        yt_service = get_youtube_service()
    except Exception:
        pass

    res = gate.replace_from_reserve(
        claimed_post_id=req.post_id,
        action_on_claimed=req.action_on_claimed,
        delete_confirmation=req.delete_confirmation,
        youtube_service=yt_service,
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Replacement failed"))
    return res


@router.post("/action-published", dependencies=[Depends(verify_rights_write_security)])
def handle_published_clip_action(req: PublishedActionRequest):
    """
    Handle action on recently published clip: keep, make_private, delete, remove_record.
    Deleting requires explicit typed confirmation "DELETE".
    """
    if req.action in ("delete", "remove_record"):
        if req.delete_confirmation != "DELETE":
            raise HTTPException(status_code=400, detail="Must type 'DELETE' to confirm deletion.")

    yt_service = None
    try:
        from dashboard.backend.youtube_api import get_youtube_service
        yt_service = get_youtube_service()
    except Exception:
        pass

    with get_rw_connection() as conn:
        if req.action == "make_private":
            if yt_service and req.video_id:
                try:
                    yt_service.videos().update(
                        part="status",
                        body={"id": req.video_id, "status": {"privacyStatus": "private"}},
                    ).execute()
                except Exception:
                    pass
            conn.execute("UPDATE uploads SET privacy = 'private', rights_status = 'held' WHERE post_id = ?", (req.post_id,))
        elif req.action in ("delete", "remove_record"):
            if yt_service and req.video_id:
                try:
                    yt_service.videos().delete(id=req.video_id).execute()
                except Exception:
                    pass
            # Permanently remove the clip from uploads and schedule tables
            conn.execute("DELETE FROM uploads WHERE post_id = ?", (req.post_id,))
            conn.execute("DELETE FROM schedule WHERE post_id = ?", (req.post_id,))
        elif req.action == "keep":
            conn.execute("UPDATE uploads SET rights_status = 'cleared', claim_status = 'none' WHERE post_id = ?", (req.post_id,))
            conn.execute("UPDATE claims SET action = 'kept' WHERE post_id = ?", (req.post_id,))

    return {"success": True, "post_id": req.post_id, "action": req.action}


@router.get("/claims")
def get_claims_history(limit: int = 50):
    """Returns list of registered claims with lifecycle state and nudge flags."""
    now = datetime.now()
    with get_ro_connection() as conn:
        cur = conn.execute("SELECT * FROM claims ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        source = r.get("source") or "manual"
        detected_at_str = r.get("detected_at") or r.get("created_at")
        r["verify_in_studio_nudge"] = False
        if source == "manual" and detected_at_str and r.get("state") == "active":
            try:
                dt = datetime.fromisoformat(detected_at_str)
                if (now - dt).days >= 7:
                    r["verify_in_studio_nudge"] = True
            except Exception:
                pass

        r["confirm_release_prompt"] = (r.get("state") == "unknown_pending_confirmation")

    return rows


@router.get("/claims-sync-status")
def get_claims_sync_status():
    """Returns status of Gmail connection, sync metrics, dry-run state, and daily action counts."""
    cfg = load_claims_config()
    is_auth = is_gmail_authenticated()

    status_file = WORKSPACE_DIR / "agent" / "data" / "claims_sync_status.json"
    sync_meta = {}
    if status_file.is_file():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                sync_meta = json.load(f)
        except Exception:
            pass

    with get_ro_connection() as conn:
        today_actions = count_today_auto_actions(conn)
        unmatched_cnt = conn.execute("SELECT COUNT(*) as cnt FROM claim_emails WHERE status = 'unmatched'").fetchone()["cnt"]
        total_processed = conn.execute("SELECT COUNT(*) as cnt FROM claim_emails").fetchone()["cnt"]

    return {
        "gmail_connected": is_auth,
        "gmail_error": sync_meta.get("gmail_error") if not is_auth else None,
        "last_sync_at": sync_meta.get("last_sync_at"),
        "processed_count": total_processed,
        "unmatched_count": unmatched_cnt,
        "dry_run": cfg.get("dry_run", True),
        "auto_action_scheduled": cfg.get("auto_action_scheduled", "delete"),
        "auto_action_public": cfg.get("auto_action_public", "delete"),
        "max_auto_actions_per_day": cfg.get("max_auto_actions_per_day", 6),
        "today_actions_count": today_actions,
    }


@router.post("/claims-sync/trigger", dependencies=[Depends(verify_rights_write_security)])
def trigger_claims_sync_now():
    """Manually trigger claims sync from the dashboard."""
    from claims_sync.sync import run_claims_sync
    res = run_claims_sync()
    return res


@router.post("/claims-sync/toggle-dry-run", dependencies=[Depends(verify_rights_write_security)])
def toggle_dry_run_endpoint(req: ToggleDryRunRequest):
    """Toggle claims dry-run mode on or off."""
    cfg = set_dry_run(req.dry_run)
    return {"success": True, "dry_run": cfg.get("dry_run", True)}


@router.post("/parse-studio-paste")
def parse_studio_paste_endpoint(req: ParseStudioPasteRequest):
    """Parse raw text copied out of YouTube Studio claim modal."""
    parsed = parse_studio_paste(req.text)
    return {"success": True, "parsed": parsed}


@router.post("/save-manual-claim", dependencies=[Depends(verify_rights_write_security)])
def save_manual_claim(req: SaveManualClaimRequest):
    """Save or update a manual claim entered via YouTube Studio paste."""
    with get_rw_connection() as conn:
        existing = conn.execute("""
            SELECT id FROM claims 
            WHERE (post_id = ? OR (video_id = ? AND video_id IS NOT NULL AND video_id != ''))
              AND state IN ('active', 'unknown_pending_confirmation')
            ORDER BY id ASC LIMIT 1
        """, (req.post_id, req.video_id)).fetchone()

        if existing:
            claim_id = existing["id"]
            conn.execute("""
                UPDATE claims
                SET claim_type = COALESCE(?, claim_type),
                    claimant = COALESCE(NULLIF(?, ''), claimant),
                    asset_title = COALESCE(NULLIF(?, ''), asset_title),
                    track_title = COALESCE(NULLIF(?, ''), track_title),
                    track_artist = COALESCE(NULLIF(?, ''), track_artist),
                    affects_reach = ?,
                    notes = COALESCE(NULLIF(?, ''), notes),
                    last_seen_at = CURRENT_TIMESTAMP,
                    state = 'active'
                WHERE id = ?
            """, (
                req.claim_type,
                req.claimant,
                req.asset_title,
                req.asset_title,
                req.track_artist,
                1 if req.affects_reach else 0,
                req.notes,
                claim_id,
            ))
        else:
            cur = conn.execute("""
                INSERT INTO claims (
                    video_id, post_id, claim_type, claimant, asset_title,
                    track_title, track_artist, affects_reach, notes, action, noted_at,
                    state, source, detected_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'held', CURRENT_TIMESTAMP, 'active', 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                req.video_id,
                req.post_id,
                req.claim_type,
                req.claimant or "Unknown Claimant",
                req.asset_title or "Unknown Asset",
                req.asset_title,
                req.track_artist,
                1 if req.affects_reach else 0,
                req.notes,
            ))
            claim_id = cur.lastrowid

        conn.execute("UPDATE uploads SET claim_status = 'claimed' WHERE post_id = ?", (req.post_id,))

    return {"success": True, "claim_id": claim_id}


@router.post("/mark-claim-released", dependencies=[Depends(verify_rights_write_security)])
def mark_claim_released_endpoint(req: MarkClaimReleasedRequest):
    """Mark a claim as released (either by claim_id or post_id)."""
    with get_rw_connection() as conn:
        if req.claim_id:
            conn.execute("""
                UPDATE claims
                SET state = 'released', released_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (req.claim_id,))
            row = conn.execute("SELECT post_id, video_id FROM claims WHERE id = ?", (req.claim_id,)).fetchone()
            if row:
                pid = row["post_id"]
                vid = row["video_id"]
                active_cnt = conn.execute("SELECT COUNT(*) as cnt FROM claims WHERE (post_id = ? OR video_id = ?) AND state = 'active'", (pid, vid)).fetchone()["cnt"]
                if active_cnt == 0:
                    conn.execute("UPDATE uploads SET claim_status = 'released' WHERE post_id = ?", (pid,))
        elif req.post_id:
            conn.execute("""
                UPDATE claims
                SET state = 'released', released_at = CURRENT_TIMESTAMP
                WHERE post_id = ?
            """, (req.post_id,))
            conn.execute("UPDATE uploads SET claim_status = 'released' WHERE post_id = ?", (req.post_id,))

    return {"success": True}


@router.post("/undo-auto-hold", dependencies=[Depends(verify_rights_write_security)])
def undo_auto_hold_endpoint(req: UndoAutoHoldRequest):
    """Undo reversible auto-hold: restore scheduled publishAt and clear claim hold."""
    res = undo_auto_hold(post_id=req.post_id)
    return res


@router.get("/claim-actions")
def get_claim_actions(limit: int = 50):
    """Returns recent audit log rows of claim actions."""
    with get_ro_connection() as conn:
        cur = conn.execute("SELECT * FROM claim_actions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


@router.get("/claim-emails/unmatched")
def get_unmatched_claim_emails(limit: int = 50):
    """Returns list of claim emails that could not be matched to pipeline uploads."""
    with get_ro_connection() as conn:
        cur = conn.execute("SELECT * FROM claim_emails WHERE status = 'unmatched' ORDER BY received_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


@router.get("/blocklist")
def get_blocklist():
    """Returns active blocklist entries and pending suggestions."""
    with get_ro_connection() as conn:
        cur = conn.execute("SELECT * FROM rights_blocklist ORDER BY id DESC")
        entries = [dict(r) for r in cur.fetchall()]

    active = [e for e in entries if e["status"] == "active"]
    pending = [e for e in entries if e["status"] == "pending_confirmation"]
    disabled = [e for e in entries if e["status"] == "disabled"]

    return {
        "active": active,
        "pending": pending,
        "disabled": disabled,
        "total_active": len(active),
        "total_pending": len(pending),
    }


@router.post("/blocklist/{entry_id}/approve", dependencies=[Depends(verify_rights_write_security)])
def approve_blocklist_suggestion(entry_id: int):
    with get_rw_connection() as conn:
        cur = conn.execute("UPDATE rights_blocklist SET status = 'active' WHERE id = ?", (entry_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Entry not found")
    return {"success": True, "entry_id": entry_id, "status": "active"}


@router.post("/blocklist/{entry_id}/dismiss", dependencies=[Depends(verify_rights_write_security)])
def dismiss_blocklist_suggestion(entry_id: int):
    with get_rw_connection() as conn:
        cur = conn.execute("UPDATE rights_blocklist SET status = 'disabled' WHERE id = ?", (entry_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Entry not found")
    return {"success": True, "entry_id": entry_id, "status": "disabled"}


@router.post("/blocklist/{entry_id}/toggle", dependencies=[Depends(verify_rights_write_security)])
def toggle_blocklist_entry(entry_id: int):
    with get_rw_connection() as conn:
        row = conn.execute("SELECT status FROM rights_blocklist WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entry not found")
        new_status = "disabled" if row["status"] == "active" else "active"
        conn.execute("UPDATE rights_blocklist SET status = ? WHERE id = ?", (new_status, entry_id))
    return {"success": True, "entry_id": entry_id, "status": new_status}


@router.post("/blocklist/add", dependencies=[Depends(verify_rights_write_security)])
def add_manual_blocklist_entry(req: AddBlocklistRequest):
    with get_rw_connection() as conn:
        cur = conn.execute("""
            INSERT INTO rights_blocklist (type, value, status)
            VALUES (?, ?, 'active')
        """, (req.type, req.value.strip()))
        entry_id = cur.lastrowid
    return {"success": True, "entry_id": entry_id}
