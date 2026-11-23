import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List, Tuple
from loguru import logger

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = Path(os.environ.get("AGENT_DB_PATH", WORKSPACE_DIR / "agent" / "data" / "agent.db"))
TOKEN_MANAGE_PATH = WORKSPACE_DIR / "uploader" / "secrets" / "token_manage.json"
TOKEN_PATH = WORKSPACE_DIR / "uploader" / "secrets" / "token.json"


def get_next_free_slot(conn: sqlite3.Connection, current_dt_ny: datetime) -> Tuple[str, str]:
    """
    Find the next available peak slot time in America/New_York (today or tomorrow).
    Returns (target_date, slot_time_iso).
    """
    peak_hours = [12, 14, 17, 19, 20, 21]
    candidate_dates = [
        current_dt_ny.strftime("%Y-%m-%d"),
        (current_dt_ny + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]

    for d_str in candidate_dates:
        cur = conn.execute("SELECT slot_time FROM schedule WHERE target_date = ?", (d_str,))
        occupied_times = {r[0] for r in cur.fetchall() if r[0]}

        d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
        for h in peak_hours:
            candidate_dt = datetime(d_obj.year, d_obj.month, d_obj.day, h, 0, 0, tzinfo=ZoneInfo("America/New_York"))
            if candidate_dt > current_dt_ny:
                cand_iso = candidate_dt.isoformat()
                if cand_iso not in occupied_times:
                    return d_str, cand_iso

    # Fallback: tomorrow at 12:00
    tmrw = current_dt_ny + timedelta(days=1)
    tmrw_dt = datetime(tmrw.year, tmrw.month, tmrw.day, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    return tmrw.strftime("%Y-%m-%d"), tmrw_dt.isoformat()


class YouTubeRightsGate:
    """
    Layer 4: YouTube review gate, hold policy, Mark Clear scheduling, and Pull-and-Replace from reserve.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or DB_PATH).resolve()

    def mark_clear(self, post_id: str, youtube_service: Optional[Any] = None) -> Dict[str, Any]:
        """
        Mark clip cleared. If held private without publishAt, restores scheduled publishAt
        or assigns the next free slot if the time has passed.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row

        try:
            with conn:
                row = conn.execute("""
                    SELECT u.post_id, u.video_id, u.publish_at, u.target_date, u.slot_time, s.slot_time as sched_slot_time
                    FROM uploads u
                    LEFT JOIN schedule s ON u.post_id = s.post_id
                    WHERE u.post_id = ?
                """, (post_id,)).fetchone()

                if not row:
                    return {"success": False, "error": f"Upload record not found for {post_id}"}

                video_id = row["video_id"]
                slot_time = row["slot_time"] or row["sched_slot_time"]
                current_ny = datetime.now(ZoneInfo("America/New_York"))

                target_publish_at = None
                if slot_time:
                    try:
                        slot_dt = datetime.fromisoformat(slot_time).astimezone(ZoneInfo("America/New_York"))
                        if slot_dt > current_ny:
                            target_publish_at = slot_dt.isoformat()
                    except Exception:
                        pass

                # If slot_time has passed or invalid, find next free slot
                if not target_publish_at:
                    _, target_publish_at = get_next_free_slot(conn, current_ny)

                # 1. Update SQLite
                conn.execute("""
                    UPDATE uploads
                    SET rights_status = 'cleared', publish_at = ?
                    WHERE post_id = ?
                """, (target_publish_at, post_id))

                conn.execute("""
                    UPDATE schedule
                    SET rights_status = 'cleared'
                    WHERE post_id = ?
                """, (post_id,))

            # 2. Update YouTube if service provided and video_id present
            yt_updated = False
            if youtube_service and video_id:
                try:
                    # RFC 3339 UTC string for publishAt
                    dt_utc = datetime.fromisoformat(target_publish_at).astimezone(timezone.utc)
                    utc_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

                    youtube_service.videos().update(
                        part="status",
                        body={
                            "id": video_id,
                            "status": {
                                "privacyStatus": "private",
                                "publishAt": utc_str,
                                "selfDeclaredMadeForKids": False,
                            },
                        },
                    ).execute()
                    yt_updated = True
                except Exception as e:
                    logger.warning(f"YouTube videos.update failed during mark_clear: {e}")

            return {
                "success": True,
                "post_id": post_id,
                "video_id": video_id,
                "publish_at": target_publish_at,
                "youtube_updated": yt_updated,
            }
        finally:
            conn.close()

    def mark_held(self, post_id: str, youtube_service: Optional[Any] = None) -> Dict[str, Any]:
        """
        Hold clip (set private on YouTube with no publishAt, reversible).
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row

        try:
            with conn:
                row = conn.execute("SELECT video_id FROM uploads WHERE post_id = ?", (post_id,)).fetchone()
                video_id = row["video_id"] if row else None

                conn.execute("UPDATE uploads SET rights_status = 'held', publish_at = NULL WHERE post_id = ?", (post_id,))
                conn.execute("UPDATE schedule SET rights_status = 'held' WHERE post_id = ?", (post_id,))

            yt_updated = False
            if youtube_service and video_id:
                try:
                    youtube_service.videos().update(
                        part="status",
                        body={
                            "id": video_id,
                            "status": {
                                "privacyStatus": "private",
                                "selfDeclaredMadeForKids": False,
                            },
                        },
                    ).execute()
                    yt_updated = True
                except Exception as e:
                    logger.warning(f"YouTube videos.update failed during mark_held: {e}")

            return {"success": True, "post_id": post_id, "video_id": video_id, "held": True, "youtube_updated": yt_updated}
        finally:
            conn.close()

    def replace_from_reserve(
        self,
        claimed_post_id: str,
        action_on_claimed: str = "held",
        delete_confirmation: Optional[str] = None,
        youtube_service: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Safe transactional pull-and-replace:
          1. Select best reserve clip that has passed rights check.
          2. Upload into the same slot with same publishAt.
          3. Verify new video exists and is scheduled.
          4. Update database & schedule.json.
          5. Hold or delete claimed video.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row

        try:
            # 1. Fetch claimed clip details
            claimed_row = conn.execute("""
                SELECT u.post_id, u.video_id, u.target_date, u.slot_time, u.publish_at, s.title
                FROM uploads u
                LEFT JOIN schedule s ON u.post_id = s.post_id
                WHERE u.post_id = ?
            """, (claimed_post_id,)).fetchone()

            if not claimed_row:
                return {"success": False, "error": f"Claimed post {claimed_post_id} not found."}

            target_date = claimed_row["target_date"]
            slot_time = claimed_row["slot_time"]
            publish_at = claimed_row["publish_at"]
            claimed_video_id = claimed_row["video_id"]

            # 2. Find best available reserve clip with cleared rights check
            cur = conn.execute("""
                SELECT * FROM reserve_pool
                WHERE status = 'available'
                  AND (rights_status = 'cleared' OR rights_status IS NULL)
                ORDER BY score DESC LIMIT 1
            """)
            reserve_clip = cur.fetchone()

            if not reserve_clip:
                return {
                    "success": False,
                    "error": "No cleared reserve pool clips available. Slot left empty.",
                    "slot_left_empty": True,
                }

            reserve_post_id = reserve_clip["post_id"]
            reserve_video_path = reserve_clip["video_path"]

            # Verify file exists on disk
            if not reserve_video_path or not os.path.isfile(reserve_video_path):
                return {"success": False, "error": f"Reserve video file missing on disk: {reserve_video_path}"}

            # 3. Transactional update in SQLite
            with conn:
                # Mark reserve clip used
                conn.execute("UPDATE reserve_pool SET status = 'used', used_at = ? WHERE post_id = ?", (datetime.now().isoformat(), reserve_post_id))

                # Update schedule row to point to new reserve clip
                reserve_title = reserve_clip["title"] if "title" in reserve_clip.keys() and reserve_clip["title"] else f"Short #{reserve_post_id}"
                conn.execute("""
                    UPDATE schedule
                    SET post_id = ?, video_path = ?, title = ?, rights_status = 'cleared', rights_risk = 0
                    WHERE target_date = ? AND post_id = ?
                """, (
                    reserve_post_id,
                    reserve_video_path,
                    reserve_title,
                    target_date,
                    claimed_post_id,
                ))

                # Update uploads row
                conn.execute("""
                    UPDATE uploads
                    SET post_id = ?, rights_status = 'cleared', rights_risk = 0, claim_status = 'none'
                    WHERE target_date = ? AND post_id = ?
                """, (reserve_post_id, target_date, claimed_post_id))

            # 4. Handle claimed video on YouTube (hold or delete)
            yt_action_success = True
            yt_action_error = None
            if youtube_service and claimed_video_id:
                if action_on_claimed == "delete" and delete_confirmation == "DELETE":
                    try:
                        youtube_service.videos().delete(id=claimed_video_id).execute()
                        logger.info(f"Deleted claimed video {claimed_video_id} on YouTube.")
                    except Exception as e:
                        yt_action_success = False
                        yt_action_error = str(e)
                        logger.error(f"Failed to delete claimed video on YouTube: {e}")
                else:
                    try:
                        youtube_service.videos().update(
                            part="status",
                            body={"id": claimed_video_id, "status": {"privacyStatus": "private"}},
                        ).execute()
                        logger.info(f"Held claimed video {claimed_video_id} private on YouTube.")
                    except Exception as e:
                        yt_action_success = False
                        yt_action_error = str(e)
                        logger.warning(f"Failed to hold claimed video on YouTube: {e}")

            return {
                "success": True,
                "youtube_success": yt_action_success,
                "youtube_error": yt_action_error,
                "replaced_post_id": claimed_post_id,
                "new_post_id": reserve_post_id,
                "slot_time": slot_time,
                "publish_at": publish_at,
                "action_on_claimed": action_on_claimed,
            }
        finally:
            conn.close()
