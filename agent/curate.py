#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║             AUTOMATED VIDEO CURATION AGENT — PHASE 3                 ║
║                                                                      ║
║  Orchestrates video curation for tomorrow using LangGraph:           ║
║    - Multi-loop discovery planning                                   ║
║    - Phase 2 Scraper ingestion                                       ║
║    - Zero-cost filters & Whisper audio VAD check                     ║
║    - Claude Vision judging with 4-6 keyframes                        ║
║    - Human-in-the-loop review with LangGraph interrupt & Rich UI    ║
║    - Viral caption generation (max 8 words)                          ║
║    - Phase 1 NVENC video editor integration                          ║
║    - 24-slot hourly schedule generation (schedule.json)              ║
╚══════════════════════════════════════════════════════════════════════╝

Usage:
    python curate.py --target 24 --date tomorrow
    python curate.py --resume
    python curate.py --dry-run
"""

import sys
import os
import json
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import yaml
from dotenv import load_dotenv
from loguru import logger
from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Add agent directory and project root to sys.path
BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(1, str(BASE_DIR.parent))

from uploader.dates import resolve_date_arg, get_ist_today

# Load .env from current working dir, agent dir, or project root
for env_candidate in [Path.cwd() / ".env", BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
    if env_candidate.is_file():
        load_dotenv(dotenv_path=env_candidate)
load_dotenv()

from graph.build_graph import build_curation_graph
from graph.state import CurationState
from llm.client import LLMClient
from tools.scraper_tool import ScraperTool
from tools.editor_tool import EditorTool
from core.db import AgentDatabase
from core.review_ui import ReviewUI

console = Console()


def load_config(config_path: Path) -> dict:
    """Load agent configuration file and sync durations from scraper config (single source of truth)."""
    if not config_path.exists():
        logger.error(f"Config file not found at {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # Synchronize duration bounds from scraper/config.yaml (single source of truth)
    scraper_cfg_path = config_path.parent.parent / "scraper" / "config.yaml"
    if scraper_cfg_path.is_file():
        try:
            with open(scraper_cfg_path, "r", encoding="utf-8") as sf:
                sc_cfg = yaml.safe_load(sf) or {}
            sc_filters = sc_cfg.get("filters", {})
            if "min_duration_seconds" in sc_filters:
                min_sec = float(sc_filters["min_duration_seconds"])
                cfg.setdefault("filters", {})["min_duration_seconds"] = min_sec
                cfg["filters"]["min_duration"] = min_sec
            if "max_duration_seconds" in sc_filters:
                max_sec = float(sc_filters["max_duration_seconds"])
                cfg.setdefault("filters", {})["max_duration_seconds"] = max_sec
                cfg["filters"]["max_duration"] = max_sec

            sc_reddit = sc_cfg.get("reddit", {})
            if sc_reddit:
                cfg.setdefault("reddit", {})
                for k in ["listings", "max_requests_per_run", "max_requests_per_day", "subreddits_per_run"]:
                    if k in sc_reddit:
                        cfg["reddit"][k] = sc_reddit[k]
        except Exception as e:
            logger.debug(f"Could not sync duration/reddit config from scraper: {e}")

    return cfg


def sync_schedule_json(target_date: str, tz_name: str, db: AgentDatabase) -> Path:
    """Sync agent/data/edited/<target_date>/schedule.json with current active schedule rows in SQLite."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    with db._get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM schedule
            WHERE target_date = ? AND status != 'empty'
            ORDER BY slot_hour ASC, slot_time ASC
        """, (target_date,)).fetchall()

    slots = []
    for r in rows:
        d = dict(r)
        slots.append({
            "slot_hour": d.get("slot_hour", 12),
            "slot_time": d.get("slot_time"),
            "post_id": d.get("post_id"),
            "video_path": d.get("video_path"),
            "title": d.get("title", ""),
            "description": d.get("description", ""),
            "caption": d.get("caption", ""),
            "source_url": d.get("source_url", ""),
            "uploader": d.get("uploader", "unknown"),
            "score": d.get("score", 0.0) if "score" in d else 0.0,
            "is_peak": True,
            "status": d.get("status", "queued"),
            "rights_status": d.get("rights_status", "cleared"),
            "rights_risk": d.get("rights_risk", 0),
        })

    schedule_path = BASE_DIR / "data" / "edited" / target_date / "schedule.json"
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    with open(schedule_path, "w", encoding="utf-8") as f:
        json.dump({
            "target_date": target_date,
            "timezone": tz_name,
            "created_at": datetime.now(tz).isoformat(),
            "total_slots": len(slots),
            "empty_slots": [],
            "slots": slots,
        }, f, indent=2)
    logger.info(f"Synchronized schedule.json at {schedule_path} ({len(slots)} slots).")
    return schedule_path


def print_final_summary(
    state: dict,
    target_count: int,
    target_date: str,
    cost_usd: float,
    llm_calls: int,
    dry_run: bool,
    schedule_path: Path,
) -> None:
    """Display final execution summary table using Rich."""
    approved_count = len(state.get("approved", []))
    rejected_count = len(state.get("rejected", []))
    scheduled_count = len(state.get("final_schedule", []))
    loops_used = state.get("loop_count", 1)

    table = Table(title="[bold green]🏆 CURATION RUN COMPLETED[/bold green]", show_header=True)
    table.add_column("Metric", style="bold white", width=28)
    table.add_column("Value", style="cyan")

    table.add_row("Target Date", f"{target_date}")
    table.add_row("Target Goal", f"{target_count} videos (1 per hour)")
    table.add_row("Curation Loops Used", f"{loops_used}")
    table.add_row("Total Approved Clips", f"{approved_count}")
    table.add_row("Total Rejected Clips", f"{rejected_count}")
    failed_count = len(state.get("judge_failed", []))
    if failed_count:
        table.add_row("Judge Failures (Retrying)", f"[yellow]{failed_count}[/yellow]")
    table.add_row("Final Scheduled Slots", f"[bold green]{scheduled_count} / {target_count}[/bold green]")
    table.add_row("LLM API Calls", f"{llm_calls}")
    table.add_row("Total LLM Cost (USD)", f"${cost_usd:.4f}")
    table.add_row("Execution Mode", "[yellow]DRY RUN[/yellow]" if dry_run else "[green]PRODUCTION[/green]")

    if schedule_path.exists() or dry_run:
        table.add_row("Schedule JSON", f"{schedule_path}")

    console.print()
    console.print(table)
    console.print()


def run_curation_graph(app, initial_state_or_input, graph_config):
    """
    Executes the LangGraph curation pipeline.
    If the graph halts at a human review interrupt, handles the ReviewUI
    in the terminal outside the graph and resumes execution via Command(resume=...).
    """
    state = app.invoke(initial_state_or_input, graph_config)

    while state.get("__interrupt__"):
        interrupt_item = state["__interrupt__"][0]
        payload = interrupt_item.value

        borderline = payload.get("borderline", [])
        auto_approved = payload.get("auto_approved", [])
        auto_approved_count = payload.get("auto_approved_count", len(auto_approved))

        logger.info(f"Human review required for {len(borderline)} borderline candidate(s).")

        # 1. Display summary of auto-approved clips
        if auto_approved:
            ReviewUI.display_auto_approved_summary(auto_approved)

        # 2. Run the interactive review loop outside the graph
        review_outcome = ReviewUI.review_batch(
            borderline_videos=borderline,
            auto_approved_count=auto_approved_count,
        )

        # 3. Resume the graph with user decisions
        logger.info("Resuming graph execution with human review decisions...")
        state = app.invoke(Command(resume=review_outcome), graph_config)

    return state


def compute_and_save_run_stats(db, final_state: dict, target_date: str) -> None:
    """Compute curation pipeline metrics and record to run_stats table."""
    raw_cands = final_state.get("raw_candidates", [])
    dropped_titles = final_state.get("dropped_titles", [])
    rejected = final_state.get("rejected", [])
    approved = final_state.get("approved", [])
    borderline = final_state.get("borderline", [])
    schedule = final_state.get("final_schedule", [])

    dropped_prescreen = len(dropped_titles)

    reason_counts = {
        "not_funny": 0,
        "non_english": 0,
        "watermark": 0,
        "duration": 0,
        "tv_film": 0,
        "other": 0,
    }

    dropped_duration = 0
    dropped_filters = 0
    rejected_judge = 0

    for r in rejected:
        reason_text = (r.get("reason") or r.get("decision_reason") or "").lower()
        if any(w in reason_text for w in ("not funny", "funny_score", "score", "threshold", "unfunny")):
            reason_counts["not_funny"] += 1
            rejected_judge += 1
        elif any(w in reason_text for w in ("english", "language", "speech")):
            reason_counts["non_english"] += 1
            dropped_filters += 1
        elif any(w in reason_text for w in ("watermark", "logo", "channel")):
            reason_counts["watermark"] += 1
            rejected_judge += 1
        elif any(w in reason_text for w in ("duration", "seconds", "too long", "too short", "length")):
            reason_counts["duration"] += 1
            dropped_duration += 1
            dropped_filters += 1
        elif any(w in reason_text for w in ("tv", "film", "movie", "broadcast")):
            reason_counts["tv_film"] += 1
            rejected_judge += 1
        else:
            reason_counts["other"] += 1
            if any(w in reason_text for w in ("filter", "resolution", "word", "low res")):
                dropped_filters += 1
            else:
                rejected_judge += 1

    source_stats = {}
    for a in approved:
        src = a.get("subreddit") or a.get("source") or "unknown"
        if src not in source_stats:
            source_stats[src] = {"judged": 0, "accepted": 0}
        source_stats[src]["accepted"] += 1
        source_stats[src]["judged"] += 1

    for r in rejected:
        src = r.get("subreddit") or r.get("source") or "unknown"
        if src in source_stats:
            source_stats[src]["judged"] += 1
        elif "funny" in (r.get("reason") or "").lower() or r.get("score"):
            if src not in source_stats:
                source_stats[src] = {"judged": 0, "accepted": 0}
            source_stats[src]["judged"] += 1

    approved_count = len(approved)
    selected_count = len(schedule)
    reserve_added = max(0, approved_count - selected_count)
    borderline_count = len(borderline)
    judged_count = approved_count + rejected_judge + borderline_count

    found_count = len(raw_cands) if raw_cands else (dropped_prescreen + len(rejected) + approved_count)
    downloaded_count = max(0, found_count - dropped_prescreen)

    stats = {
        "target_date": target_date,
        "found": found_count,
        "dropped_prescreen": dropped_prescreen,
        "dropped_duration": dropped_duration,
        "downloaded": downloaded_count,
        "dropped_filters": dropped_filters,
        "judged": judged_count,
        "rejected_judge": rejected_judge,
        "borderline": borderline_count,
        "approved": approved_count,
        "selected": selected_count,
        "reserve_added": reserve_added,
        "reject_reasons_json": reason_counts,
        "source_stats_json": source_stats,
    }

    try:
        db.record_run_stats(stats)
        logger.info(f"Recorded run_stats for {target_date}: {found_count} found, {len(rejected)} rejected, {approved_count} accepted.")
    except Exception as e:
        logger.warning(f"Could not record run_stats: {e}")


PROTECTED_TODAY_PIDS = {
    "reddit_1xvojvg",
    "reddit_1xvw7dc",
    "reddit_1xvvnmb",
    "reddit_1xvh7rc",
    "reddit_1xv78q4",
    "reddit_1xvvfpr",
}


def release_orphan_reserve(db: AgentDatabase, dry_run: bool = False) -> List[Dict[str, Any]]:
    """
    Reconciles reserve_pool clips: any reserve row with status 'used' whose post_id
    is NOT in the schedule table (any date) and NOT in uploads, and whose raw file
    still exists on disk, goes back to 'available'.
    Never touches today's batch (2026-11-23).
    """
    logger.info("Checking for orphan/burned reserve pool clips...")
    with db._get_connection() as conn:
        sched_ids = set(r[0] for r in conn.execute("SELECT post_id FROM schedule WHERE post_id IS NOT NULL").fetchall())
        upload_ids = set(r[0] for r in conn.execute("SELECT post_id FROM uploads WHERE post_id IS NOT NULL").fetchall())
        used_rows = [dict(r) for r in conn.execute("SELECT * FROM reserve_pool WHERE status = 'used'").fetchall()]

    recovered = []
    table = Table(title="Orphan Reserve Pool Reconciliation", show_header=True, header_style="bold cyan")
    table.add_column("Post ID", style="dim")
    table.add_column("Old Status")
    table.add_column("New Status", style="green")
    table.add_column("Reason")

    for r in used_rows:
        pid = r["post_id"]
        if pid in PROTECTED_TODAY_PIDS:
            continue
        if pid in sched_ids or pid in upload_ids:
            continue

        raw_path = (r.get("video_path") or "").replace("python tut", "python_tut")
        if not raw_path or not Path(raw_path).is_file():
            continue

        new_status = "available" if not dry_run else "available (dry-run)"
        reason = f"Not scheduled/uploaded; raw media exists ({Path(raw_path).name})"
        table.add_row(pid, "used", new_status, reason)
        recovered.append({
            "post_id": pid,
            "old_status": "used",
            "new_status": "available",
            "reason": reason,
            "video_path": raw_path,
        })

    console = Console()
    if recovered:
        console.print(table)
        if not dry_run:
            with db._get_connection() as conn:
                for item in recovered:
                    conn.execute(
                        "UPDATE reserve_pool SET status = 'available', used_at = NULL WHERE post_id = ?",
                        (item["post_id"],)
                    )
            logger.success(f"Successfully released {len(recovered)} orphan reserve clip(s) back to 'available'.")
        else:
            logger.info(f"[DRY-RUN] Found {len(recovered)} orphan reserve clip(s) to restore. No changes written.")
    else:
        logger.info("No orphan reserve clips found. All used clips are correctly accounted for.")

    return recovered


def rescreen_reserve(db: AgentDatabase, config: Optional[Dict[str, Any]] = None, dry_run: bool = False) -> List[Dict[str, Any]]:
    """
    Runs full rights check on every reserve clip with status 'available',
    spaced by Shazam spacing, and marks blocked / cleared / needs_check.
    Skips clips in schedule table for date >= today.
    """
    from core.rights.engine import RightsCheckEngine
    logger.info("Starting reserve pool rights re-screening...")

    tz_name = (config or {}).get("timezone", "America/New_York")
    try:
        today_str = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        today_str = datetime.now().strftime("%Y-%m-%d")

    with db._get_connection() as conn:
        active_sched = set(r[0] for r in conn.execute(
            "SELECT post_id FROM schedule WHERE target_date >= ? AND post_id IS NOT NULL", (today_str,)
        ).fetchall())
        candidates = [dict(r) for r in conn.execute("SELECT * FROM reserve_pool WHERE status = 'available'").fetchall()]

    rights_engine = RightsCheckEngine(config=config, db=db)
    results = []

    table = Table(title="Reserve Pool Rights Re-screening", show_header=True, header_style="bold magenta")
    table.add_column("Post ID", style="dim")
    table.add_column("Old Status")
    table.add_column("New Status")
    table.add_column("Reason")

    for r in candidates:
        pid = r["post_id"]
        if pid in PROTECTED_TODAY_PIDS or pid in active_sched:
            logger.info(f"Skipping scheduled clip {pid} from re-screening (currently needed).")
            continue

        raw_path = (r.get("video_path") or "").replace("python tut", "python_tut")
        if not raw_path or not Path(raw_path).is_file():
            if not dry_run:
                db.mark_reserve_missing([pid])
            table.add_row(pid, "available", "missing", "Raw file not found on disk")
            results.append({"post_id": pid, "old_status": "available", "new_status": "missing", "reason": "missing_file"})
            continue

        eval_res = rights_engine.evaluate_clip(
            post_id=pid,
            video_path=raw_path,
            title=r.get("title", ""),
            uploader=r.get("uploader", ""),
            source_url=r.get("post_url", ""),
            duration=r.get("duration"),
        )

        dec = eval_res.get("decision", "pass")
        reasons = ", ".join(eval_res.get("reasons", [])) or "cleared"

        if dec == "block":
            new_st = "blocked"
            if not dry_run:
                db.mark_reserve_blocked(
                    post_id=pid,
                    reason=reasons,
                    risk_score=eval_res.get("risk_score", 100),
                    checked_at=eval_res.get("checked_at"),
                    rights_version=2,
                )
        elif dec == "review":
            new_st = "needs_check"
        else:
            new_st = "cleared"
            if not dry_run:
                db.mark_reserve_cleared(
                    post_id=pid,
                    risk_score=eval_res.get("risk_score", 0),
                    checked_at=eval_res.get("checked_at"),
                    rights_version=2,
                )

        table.add_row(pid, "available", new_st if not dry_run else f"{new_st} (dry-run)", reasons)
        results.append({
            "post_id": pid,
            "old_status": "available",
            "new_status": new_st,
            "reason": reasons,
        })

        if not dry_run:
            import time
            time.sleep(3.0)

    console = Console()
    console.print(table)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Phase 3 Curation Agent — Automated Video Selection & Scheduling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", type=int, default=6, help="Target number of scheduled videos (default: 6)")
    parser.add_argument("--date", type=str, default="tomorrow", help="Target date ('tomorrow' or 'YYYY-MM-DD')")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without modifying files or calling paid APIs")
    parser.add_argument("--no-review", action="store_true", help="Unattended mode: auto-resolve borderline clips without interactive terminal prompts")
    parser.add_argument(
        "--borderline-policy",
        choices=["reject", "approve_top", "review"],
        default=None,
        help="Policy for borderline clips: reject, approve_top, or review (default: review when interactive, approve_top when --no-review)",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from previous SQLite checkpoint")
    parser.add_argument("--fill-slots", type=str, nargs="?", const="today", default=None, help="Fill empty or held slots for a date ('today', 'tomorrow', or 'YYYY-MM-DD')")
    parser.add_argument(
        "--stock-reserve",
        type=int,
        nargs="?",
        const=12,
        default=None,
        help="Inspect and stock reserve pool up to N clips (default: 12) from available raw media or curation",
    )
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--workers", type=int, default=None, help="Parallel video editing workers")
    parser.add_argument("--recursion-limit", type=int, default=None, help="LangGraph recursion limit (default: 100)")
    parser.add_argument(
        "--release-orphan-reserve",
        action="store_true",
        help="Reconcile and restore burned reserve clips that were marked 'used' but never scheduled or uploaded",
    )
    parser.add_argument(
        "--rescreen-reserve",
        action="store_true",
        help="Re-screen all available reserve clips through the updated rights engine (Shazam)",
    )

    args = parser.parse_args()

    # Maintenance tasks do not require LLM API key
    if args.release_orphan_reserve or args.rescreen_reserve:
        pass
    elif not args.dry_run:
        has_gemini = bool(
            (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY") or "").strip("'\" ")
        )
        has_anthropic = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip("'\" "))
        if not (has_gemini or has_anthropic):
            logger.error("=" * 60)
            logger.error("FATAL ERROR: LLM API Key is missing!")
            logger.error("A real curation run (without --dry-run) requires GEMINI_API_KEY, GOOGLE_API_KEY, or ANTHROPIC_API_KEY in your .env file.")
            logger.error("Please add your key to .env:")
            logger.error("    GEMINI_API_KEY=AQ.Ab8... (or your Google AI Studio / Gemini key)")
            logger.error("    ANTHROPIC_API_KEY=sk-ant-api03-...")
            logger.error("Or run in simulation mode with:")
            logger.error("    python curate.py --dry-run")
            logger.error("=" * 60)
            sys.exit(1)

    # 1. Load Config & Environment
    config_path = BASE_DIR / args.config
    config = load_config(config_path)

    # Resolve Date & Timezone
    tz_name = config.get("timezone", "America/New_York")
    if args.fill_slots:
        target_date = resolve_date_arg(args.fill_slots, tz_name)
    else:
        target_date = resolve_date_arg(args.date, tz_name)

    # 2. Setup SQLite Checkpointer & Database
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_db_path = data_dir / "checkpoints.db"
    agent_db_path = Path(os.environ.get("AGENT_DB_PATH", data_dir / "agent.db"))

    checkpointer_conn = sqlite3.connect(str(checkpoints_db_path), check_same_thread=False)
    db = AgentDatabase(agent_db_path)

    # Handle --release-orphan-reserve
    if args.release_orphan_reserve:
        release_orphan_reserve(db, dry_run=args.dry_run)
        sys.exit(0)

    # Handle --rescreen-reserve
    if args.rescreen_reserve:
        rescreen_reserve(db, config=config, dry_run=args.dry_run)
        sys.exit(0)

    # Auto reconcile orphan reserve clips at start of curation
    if not args.dry_run and not args.fill_slots and args.stock_reserve is None:
        release_orphan_reserve(db, dry_run=False)

    logger.info("============================================================")
    if args.fill_slots:
        logger.info(f"  PHASE 3: FILLING MISSING SLOTS FOR {target_date} ({tz_name})")
    else:
        logger.info("  PHASE 3: LANGGRAPH VIDEO CURATION AGENT")
        logger.info(f"  Target: {args.target} clips | Date: {target_date} ({tz_name}) | Dry-run: {args.dry_run}")
    logger.info("============================================================")

    # Handle --stock-reserve
    if args.stock_reserve is not None:
        target_stock = int(args.stock_reserve)
        logger.info("============================================================")
        logger.info(f"  RESERVE POOL STOCK MANAGEMENT (Target: {target_stock} clips)")
        logger.info("============================================================")
        with db._get_connection() as conn:
            cur_available = conn.execute("SELECT COUNT(*) as cnt FROM reserve_pool WHERE status = 'available'").fetchone()["cnt"]
        logger.info(f"Current available clips in reserve pool: {cur_available}/{target_stock}")

        if cur_available >= target_stock:
            logger.success(f"Reserve pool is already adequately stocked ({cur_available} available >= {target_stock} target).")
            sys.exit(0)

        deficit = target_stock - cur_available
        logger.info(f"Stocking reserve pool with {deficit} clip(s)...")

        # Step A: Scan local raw media and videos.db for un-used videos
        stocked_from_disk = 0
        vdb_path = BASE_DIR.parent / "scraper" / "data" / "videos.db"
        if vdb_path.is_file():
            try:
                con_v = sqlite3.connect(str(vdb_path))
                con_v.row_factory = sqlite3.Row
                con_v.execute("UPDATE videos SET file_path = REPLACE(file_path, 'python tut', 'python_tut') WHERE file_path LIKE '%python tut%'")
                con_v.commit()

                with db._get_connection() as conn:
                    sched_ids = set(r[0] for r in conn.execute("SELECT post_id FROM schedule WHERE post_id IS NOT NULL"))
                    upload_ids = set(r[0] for r in conn.execute("SELECT post_id FROM uploads WHERE post_id IS NOT NULL"))
                    res_ids = set(r[0] for r in conn.execute("SELECT post_id FROM reserve_pool"))
                    taken_ids = sched_ids | upload_ids | res_ids

                v_rows = con_v.execute("SELECT * FROM videos WHERE file_path IS NOT NULL").fetchall()
                for r in v_rows:
                    if stocked_from_disk >= deficit:
                        break
                    pid = r["post_id"]
                    if pid in taken_ids:
                        continue
                    fp = r["file_path"].replace("python tut", "python_tut")
                    if not Path(fp).is_file():
                        continue
                    dur = r["duration"]
                    if not dur or dur < 5.0 or dur > 60.0:
                        continue

                    title = r["title"] or f"Comedy Clip #{pid}"
                    row_dict = dict(r)
                    db.add_to_reserve_pool({
                        "post_id": pid,
                        "video_path": fp,
                        "caption": title,
                        "title": title,
                        "description": title,
                        "uploader": r["uploader"] or "unknown",
                        "post_url": r["post_url"] or "",
                        "score": 8.0,
                        "duration": dur,
                        "resolution": row_dict.get("resolution") or "720x1280",
                    })
                    with db._get_connection() as conn:
                        conn.execute("UPDATE reserve_pool SET rights_status = 'cleared', rights_risk = 0 WHERE post_id = ?", (pid,))
                    taken_ids.add(pid)
                    stocked_from_disk += 1
                    logger.info(f"Stocked reserve clip from disk: {pid} - {title[:50]} ({dur:.1f}s)")
                con_v.close()
            except Exception as e:
                logger.warning(f"Error scanning local disk for reserve clips: {e}")

        with db._get_connection() as conn:
            new_available = conn.execute("SELECT COUNT(*) as cnt FROM reserve_pool WHERE status = 'available'").fetchone()["cnt"]
        logger.info(f"Reserve pool stock updated: {new_available}/{target_stock} available (added {stocked_from_disk} from disk).")

        if new_available >= target_stock:
            logger.success(f"Reserve stock goal reached ({new_available}/{target_stock} clips ready).")
            sys.exit(0)

        # Step B: If still short, run curation targeting remaining deficit
        remaining_needed = target_stock - new_available
        logger.info(f"Reserve pool still short {remaining_needed} clips. Curating additional candidates...")
        args.target = remaining_needed
        args.no_review = True

    # Handle --fill-slots
    if args.fill_slots:
        target_date = resolve_date_arg(args.fill_slots, tz_name)
        logger.info(f"Checking empty/held slots for date: {target_date}...")
        
        target_total = config.get("target_count", 6)
        raw_peak_hours = config.get("peak_hours", [12, 14, 17, 19, 20, 21])
        from graph.nodes.schedule import select_target_slots
        expected_slots = select_target_slots(target_total, raw_peak_hours)

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        with db._get_connection() as conn:
            sched_rows = [dict(r) for r in conn.execute("SELECT * FROM schedule WHERE target_date = ?", (target_date,)).fetchall()]

        sched_by_time = {r["slot_time"]: r for r in sched_rows}
        sched_by_hour = {r["slot_hour"]: r for r in sched_rows}

        missing_slots = []
        for s in expected_slots:
            hour, minute = s
            slot_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(
                hour=hour, minute=minute, second=0, tzinfo=tz
            )
            slot_time_iso = slot_dt.isoformat()
            existing = sched_by_time.get(slot_time_iso) or sched_by_hour.get(hour)
            if not existing or existing.get("status") == "empty" or existing.get("rights_status") == "held":
                missing_slots.append({
                    "slot_hour": hour,
                    "slot_minute": minute,
                    "slot_time": slot_time_iso,
                    "existing": existing
                })

        if not missing_slots:
            logger.info(f"All {target_total} slots for {target_date} are filled. Nothing to do.")
            sync_schedule_json(target_date, tz_name, db)
            sys.exit(0)

        logger.info(f"Found {len(missing_slots)} missing/empty/held slot(s) for {target_date}.")

        # 1. Try filling from already-branded videos on disk that aren't yet scheduled
        edited_dir = BASE_DIR / "data" / "edited" / target_date
        unassigned_branded = []
        if edited_dir.is_dir():
            scheduled_pids = {r["post_id"] for r in sched_rows if r.get("post_id")}
            for bf in sorted(edited_dir.glob("*_branded.mp4")):
                pid = bf.stem.replace("_branded", "")
                if pid not in scheduled_pids and bf.stat().st_size > 100_000:
                    unassigned_branded.append((pid, bf))

        # 2. Try filling from reserve pool
        with db._get_connection() as conn:
            res_clips = [dict(r) for r in conn.execute("""
                SELECT * FROM reserve_pool
                WHERE status = 'available' AND (rights_status = 'cleared' OR rights_status IS NULL)
                ORDER BY score DESC
            """).fetchall()]

        filled_any = False
        for ms in list(missing_slots):
            if unassigned_branded:
                pid, bf = unassigned_branded.pop(0)
                with db._get_connection() as conn:
                    rp_row = conn.execute("SELECT * FROM reserve_pool WHERE post_id = ?", (pid,)).fetchone()
                btitle = (rp_row["title"] if rp_row and rp_row["title"] else f"Short #{pid}")
                uploader = (rp_row["uploader"] if rp_row and rp_row["uploader"] else "unknown")
                with db._get_connection() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO schedule (
                            target_date, slot_hour, slot_time, post_id, video_path, title, description, caption,
                            source_url, uploader, status, rights_status, rights_risk, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, 'queued', 'cleared', 0, CURRENT_TIMESTAMP)
                    """, (target_date, ms["slot_hour"], ms["slot_time"], pid, str(bf), btitle, btitle, btitle, uploader))
                    conn.execute("UPDATE reserve_pool SET status = 'used', used_at = CURRENT_TIMESTAMP WHERE post_id = ?", (pid,))
                logger.info(f"Filled slot {ms['slot_time']} with existing branded video {pid}.")
                missing_slots.remove(ms)
                filled_any = True
            elif res_clips:
                rc = res_clips.pop(0)
                with db._get_connection() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO schedule (
                            target_date, slot_hour, slot_time, post_id, video_path, title, description, caption,
                            source_url, uploader, status, rights_status, rights_risk, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'cleared', 0, CURRENT_TIMESTAMP)
                    """, (target_date, ms["slot_hour"], ms["slot_time"], rc["post_id"], rc["video_path"], rc.get("title", f"Short #{rc['post_id']}"), rc.get("description", ""), rc.get("caption", ""), rc.get("post_url", ""), rc.get("uploader", "unknown")))
                    conn.execute("UPDATE reserve_pool SET status = 'used', used_at = CURRENT_TIMESTAMP WHERE post_id = ?", (rc["post_id"],))
                logger.info(f"Filled slot {ms['slot_time']} with reserve clip {rc['post_id']}.")
                missing_slots.remove(ms)
                filled_any = True

        if filled_any:
            sync_schedule_json(target_date, tz_name, db)

        if not missing_slots:
            logger.info(f"All slots for {target_date} successfully backfilled!")
            sys.exit(0)

        # Still empty slots; proceed to run curation for remaining
        args.date = target_date
        args.target = len(missing_slots)
        args.no_review = True

    # 3. Initialize Core Tools & LLM Client
    parallel_edits = args.workers or config.get("parallel_edits", 2)
    llm_client = LLMClient(config=config, dry_run=args.dry_run, db_handler=db)
    scraper_tool = ScraperTool()
    editor_tool = EditorTool(max_workers=parallel_edits)

    # 4. Build LangGraph
    app = build_curation_graph(
        config=config,
        llm_client=llm_client,
        scraper_tool=scraper_tool,
        editor_tool=editor_tool,
        db=db,
        checkpointer_conn=checkpointer_conn,
    )

    thread_id = f"curate_{target_date}"
    recursion_limit = args.recursion_limit or config.get("recursion_limit", 100)
    graph_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }

    # Resolve borderline policy default: review if interactive, approve_top if --no-review
    resolved_borderline_policy = args.borderline_policy or ("approve_top" if args.no_review else "review")

    # 5. Initialize or Resume State
    if args.resume:
        logger.info(f"Resuming graph execution for thread: {thread_id} (recursion_limit: {recursion_limit})...")
        final_state = run_curation_graph(app, None, graph_config)
    else:
        initial_state: CurationState = {
            "target_count": args.target,
            "target_date": target_date,
            "loop_count": 0,
            "max_loops": config.get("max_loops", 3),
            "spare_buffer": config.get("spare_buffer", 2),
            "candidates": [],
            "approved": [],
            "borderline": [],
            "rejected": [],
            "sources_tried": [],
            "plan": {},
            "cost_so_far": 0.0,
            "llm_calls_count": 0,
            "backfill_performed": False,
            "trigger_extra_loop": False,
            "human_interrupted": False,
            "dry_run": args.dry_run,
            "no_review": args.no_review,
            "borderline_policy": resolved_borderline_policy,
            "scraper_blocked": False,
            "scraper_error": False,
            "discovered": False,
            "selected_sources": [],
            "raw_candidates": [],
            "vetted_candidates": [],
            "dropped_titles": [],
            "edited_videos": [],
            "final_schedule": [],
        }
        final_state = run_curation_graph(app, initial_state, graph_config)

    # 6. Final Summary Report
    schedule_path = BASE_DIR / "data" / "edited" / target_date / "schedule.json"
    print_final_summary(
        state=final_state,
        target_count=args.target,
        target_date=target_date,
        cost_usd=llm_client.total_cost_usd,
        llm_calls=llm_client.call_count,
        dry_run=args.dry_run,
        schedule_path=schedule_path,
    )

    # 6.5 Record Run Stats (Phase 5 Dashboard Instrumentation)
    compute_and_save_run_stats(db=db, final_state=final_state, target_date=target_date)

    # 7. Optional Telegram Notification
    try:
        from core.telegram import notify_curation_complete
        notify_curation_complete(
            target_date=target_date,
            scheduled_count=len(final_state.get("final_schedule", [])),
            target_count=args.target,
            approved_count=len(final_state.get("approved", [])),
            rejected_count=len(final_state.get("rejected", [])),
            dry_run=args.dry_run,
            schedule_path=str(schedule_path) if schedule_path.exists() else None,
        )
    except Exception as e:
        logger.debug(f"Telegram notification skipped: {e}")

    checkpointer_conn.close()


if __name__ == "__main__":
    main()
