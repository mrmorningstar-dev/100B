from pathlib import Path
from typing import Dict, Any, List
from loguru import logger
from graph.state import CurationState
from core.db import AgentDatabase
from core.rights import RightsCheckEngine
from graph.nodes.caption import is_generic_title


def backfill_node(state: CurationState, db: AgentDatabase) -> Dict[str, Any]:
    """
    Backfills approved videos from the SQLite reserve pool if approved < target_count.
    Verifies that reserve video files actually exist on disk before approving them,
    skipping and marking any missing files to prevent Phase 4 publishing crashes.
    If the reserve pool is insufficient to cover the shortage, triggers one extra loop
    through the PLAN node.

    State changes (approved, backfill_performed, trigger_extra_loop) are returned as
    explicit node outputs so LangGraph persists them across node boundaries.
    """
    if state.get("human_interrupted", False):
        return {}

    dry_run = state.get("dry_run", False)
    approved = list(state.get("approved", []))
    target_count = state.get("target_count", 6)

    # First, verify any already-approved reserve clips in state still exist on disk
    valid_existing_approved = []
    for vid in approved:
        v_path = vid.get("edited_video_path") or vid.get("video_path")
        if vid.get("already_edited") or vid.get("is_reserve_backfill"):
            if not v_path or not Path(v_path).is_file():
                logger.warning(
                    f"[Node: BACKFILL] Existing approved reserve clip '{vid.get('post_id')}' "
                    f"file missing from disk: {v_path}. Dropping from approved."
                )
                continue
        valid_existing_approved.append(vid)

    approved = valid_existing_approved
    shortage = target_count - len(approved)

    # If already performed backfill in a prior pass, do not backfill or trigger extra loop again
    if state.get("backfill_performed", False):
        logger.info(
            f"--- [Node: BACKFILL] Already performed in earlier loop. "
            f"Final approved count: {len(approved)}/{target_count} ---"
        )
        return {
            "approved": approved,
            "trigger_extra_loop": False,
        }

    logger.info(f"--- [Node: BACKFILL] Current approved videos: {len(approved)}/{target_count} ---")

    if shortage > 0:
        logger.warning(f"Shortage of {shortage} clips. Attempting reserve pool backfill...")
        # Fetch extra candidates to account for potential missing/deleted files
        candidate_reserves = db.fetch_reserve_pool_candidates(limit=max(shortage * 5, 20))
        valid_reserves: List[Dict[str, Any]] = []
        missing_ids: List[str] = []

        for r in candidate_reserves:
            v_path = r.get("video_path") or r.get("edited_video_path")
            file_exists = False
            if v_path:
                try:
                    file_exists = Path(v_path).is_file()
                except Exception:
                    file_exists = False

            if not file_exists:
                logger.warning(
                    f"[Node: BACKFILL] Reserve clip '{r.get('post_id')}' file not found on disk: {v_path}. "
                    "Skipping and marking as missing."
                )
                missing_ids.append(r["post_id"])
                continue

            # Verify reserve clip rights status (re-run if missing or stale > 7 days)
            rights_status = r.get("rights_status")
            rights_checked_at = r.get("rights_checked_at")
            is_stale = True
            if rights_checked_at:
                try:
                    from datetime import datetime, timedelta
                    dt_check = datetime.fromisoformat(rights_checked_at)
                    if datetime.now() - dt_check < timedelta(days=7):
                        is_stale = False
                except Exception:
                    is_stale = True

            if not rights_status or is_stale:
                rights_engine = RightsCheckEngine(config=state.get("config", {}), db=db)
                eval_res = rights_engine.evaluate_clip(
                    post_id=r["post_id"],
                    video_path=v_path,
                    title=r.get("title", ""),
                    uploader=r.get("uploader", ""),
                    source_url=r.get("post_url", ""),
                    duration=r.get("duration"),
                )
                if eval_res["decision"] == "block":
                    logger.warning(
                        f"[Node: BACKFILL] Reserve clip '{r.get('post_id')}' failed rights check: "
                        f"{eval_res.get('reasons')}. Skipping candidate."
                    )
                    continue
                r["rights_status"] = "needs_check" if eval_res["decision"] == "review" else "cleared"
                r["rights_risk"] = eval_res["risk_score"]
                r["rights_checked_at"] = eval_res["checked_at"]

            valid_reserves.append(r)
            if len(valid_reserves) == shortage:
                break

        # Mark missing files in database so they are never queried again
        if missing_ids and hasattr(db, "mark_reserve_missing"):
            db.mark_reserve_missing(missing_ids)

        if valid_reserves:
            logger.info(f"Backfilling {len(valid_reserves)} verified raw videos from reserve pool.")
            for r in valid_reserves:
                r["status"] = "reserve_backfill"
                r["is_reserve_backfill"] = True
                # Reserve pool clips keep raw media and are edited fresh with current branding when scheduled
                r["already_edited"] = False
                raw_path = r.get("raw_video_path") or r.get("video_path")
                r["video_path"] = raw_path
                r["raw_video_path"] = raw_path
                r.pop("edited_video_path", None)

                res_title = r.get("title")
                if is_generic_title(res_title):
                    r["youtube_title"] = None
                else:
                    r["youtube_title"] = res_title
                if "description" in r and not r.get("youtube_description"):
                    r["youtube_description"] = r["description"]
                if "caption" in r and not r.get("caption"):
                    r["caption"] = r.get("caption")
                approved.append(r)
            db.mark_reserve_used([r["post_id"] for r in valid_reserves])

        remaining_shortage = target_count - len(approved)
        if remaining_shortage > 0:
            if state.get("scraper_blocked") or state.get("scraper_error"):
                logger.info(
                    f"Reserve pool insufficient (short {remaining_shortage}), but scraper is blocked or crashed. "
                    "Not triggering extra loop through PLAN."
                )
                return {
                    "approved": approved,
                    "backfill_performed": True,
                    "trigger_extra_loop": False,
                }
            logger.info(
                f"Reserve pool insufficient (still short {remaining_shortage}). "
                "Triggering one final extra loop through PLAN."
            )
            return {
                "approved": approved,
                "backfill_performed": True,
                "trigger_extra_loop": True,
            }
        else:
            logger.info(f"Target count satisfied after reserve backfill ({len(approved)}/{target_count}).")
            return {
                "approved": approved,
                "backfill_performed": True,
                "trigger_extra_loop": False,
            }

    logger.info("Target count satisfied. No reserve backfill needed.")
    return {
        "approved": approved,
        "backfill_performed": True,
        "trigger_extra_loop": False,
    }

