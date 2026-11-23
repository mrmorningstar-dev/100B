#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║               AUTOMATED VIDEO SCRAPER — PHASE 2 & 3.5                ║
║                                                                      ║
║  Discovers, filters, deduplicates, and downloads funny videos        ║
║  from Reddit and X (Twitter) with punchy social captions.            ║
║                                                                      ║
║  Usage:                                                              ║
║    python scrape.py --target 40 --sources reddit                     ║
║    python scrape.py --list-only --candidates-output cands.json       ║
║    python scrape.py --download-only --candidates-file cands.json     ║
║    python scrape.py --dry-run                                        ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import sys
import os
import argparse
import time
import json
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
import yaml
from dotenv import load_dotenv
from loguru import logger

# Add scraper root to sys.path
BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

# Load .env from current dir, scraper dir, or project root
for env_candidate in [Path.cwd() / ".env", BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
    if env_candidate.is_file():
        load_dotenv(dotenv_path=env_candidate)
load_dotenv()

from core import (
    VideoDatabase,
    Deduplicator,
    VideoDownloader,
    check_candidate_pre_download,
    rank_candidates,
)
from sources import get_source_plugin, SOURCES
from sources.base import VideoCandidate


def setup_logging(logs_dir: Path, date_str: str) -> None:
    """Configure loguru logging with rotation and clean console output."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"scraper_{date_str}.log"

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level="INFO",
    )
    logger.add(
        str(log_file),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        rotation="50 MB",
        encoding="utf-8",
    )


def load_yaml_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration YAML file."""
    path = Path(config_path)
    if not path.is_file():
        path = BASE_DIR / config_path
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_videos_for_date(date_str: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Importable query function for Phase 3 (YouTube Uploader / Agent).
    Returns list of ready-to-publish videos with caption, metadata, and absolute paths.
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    resolved_db = db_path or str(BASE_DIR / "data" / "videos.db")
    db = VideoDatabase(resolved_db)
    return db.get_videos_for_date(date_str)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Automated Funny Video Scraper (Reddit & X)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", "-t", type=int, default=40,
        help="Target number of unique videos to download (default: 40)",
    )
    parser.add_argument(
        "--sources", "-s", default="reddit",
        help="Comma-separated sources to scrape: reddit,twitter (default: reddit)",
    )
    parser.add_argument(
        "--config", "-c", default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--date", "-d", default=None,
        help="Target date string YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Find and filter candidates without downloading media",
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help="Only list, filter, deduplicate and rank candidates; save JSON without downloading media",
    )
    parser.add_argument(
        "--candidates-output", default=None,
        help="Path to save candidate JSON file for --list-only mode",
    )
    parser.add_argument(
        "--download-only", action="store_true",
        help="Skip scraping; download media for candidates listed in --candidates-file",
    )
    parser.add_argument(
        "--candidates-file", default=None,
        help="Path to candidate JSON file to download for --download-only mode",
    )
    parser.add_argument(
        "--sources-json", default=None,
        help="JSON string or file path specifying custom sources (type, name, listing, cursor)",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Number of concurrent download threads (overrides config.yaml)",
    )
    parser.add_argument(
        "--subreddits", default=None,
        help="Comma-separated subreddits to override config (e.g. funny,Unexpected)",
    )
    parser.add_argument(
        "--twitter-accounts", default=None,
        help="Comma-separated Twitter accounts to override config (e.g. DailyViralVids,LaughsDaily)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass circuit breaker and force scrape execution",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    config = load_yaml_config(args.config)
    config["force"] = bool(args.force)

    # Override subreddits / twitter accounts in config if passed via CLI
    if args.subreddits:
        subs = [s.strip() for s in args.subreddits.split(",") if s.strip()]
        if subs:
            config.setdefault("reddit", {})["subreddits"] = subs
            logger.info(f"Overriding Reddit subreddits from CLI: {subs}")

    if args.twitter_accounts:
        accounts = [a.strip() for a in args.twitter_accounts.split(",") if a.strip()]
        if accounts:
            config.setdefault("twitter", {})["accounts"] = accounts
            logger.info(f"Overriding Twitter accounts from CLI: {accounts}")

    if args.sources_json:
        try:
            s_val = args.sources_json.strip()
            if Path(s_val).is_file():
                with open(s_val, "r", encoding="utf-8") as f:
                    custom_srcs = json.load(f)
            else:
                custom_srcs = json.loads(s_val)
            if isinstance(custom_srcs, list):
                config.setdefault("reddit", {})["custom_sources"] = custom_srcs
                config["custom_sources"] = custom_srcs
                logger.info(f"Injected {len(custom_srcs)} custom sources from --sources-json")
        except Exception as e:
            logger.warning(f"Could not parse --sources-json: {e}")

    # Directories
    data_dir = BASE_DIR / config.get("general", {}).get("data_dir", "data")
    logs_dir = BASE_DIR / config.get("general", {}).get("logs_dir", "logs")
    raw_date_dir = data_dir / "raw" / date_str
    raw_date_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(logs_dir, date_str)

    logger.info("=" * 60)
    logger.info("  AUTOMATED VIDEO SCRAPER — PHASE 2 & 3.5")
    mode_desc = "DOWNLOAD-ONLY" if args.download_only else ("LIST-ONLY" if args.list_only else ("DRY-RUN" if args.dry_run else "FULL"))
    logger.info(f"  Mode: {mode_desc} | Target: {args.target} | Date: {date_str}")
    logger.info("=" * 60)

    # Database & Deduplication
    db_path = str(data_dir / "videos.db")
    db = VideoDatabase(db_path)
    dedup = Deduplicator(
        db,
        phash_threshold=config.get("deduplication", {}).get("phash_threshold", 6),
    )
    downloader = VideoDownloader(db, dedup, config)

    # --- MODE 1: DOWNLOAD-ONLY ---
    if args.download_only:
        if not args.candidates_file:
            logger.error("--download-only requires --candidates-file. Exiting.")
            sys.exit(1)

        cands_path = Path(args.candidates_file)
        if not cands_path.is_file():
            logger.error(f"Candidates file not found: {cands_path}")
            sys.exit(1)

        with open(cands_path, "r", encoding="utf-8") as f:
            candidates_data = json.load(f)

        candidates_to_download: List[VideoCandidate] = []
        for item in candidates_data:
            try:
                candidates_to_download.append(VideoCandidate(**item))
            except Exception as e:
                logger.warning(f"Error parsing candidate item: {e}")

        logger.info(f"Loaded {len(candidates_to_download)} vetted candidates from {cands_path}")
        workers = args.workers or config.get("general", {}).get("parallel_downloads", 4)
        logger.info(f"Starting parallel downloader with {workers} workers (target: {args.target})...")

        stats = {
            "found_by_source": {},
            "filtered": {},
            "id_duplicates": 0,
            "media_duplicates": 0,
            "downloaded_by_source": {},
            "failed_by_source": {},
            "dropped_too_long": 0,
            "dropped_too_short": 0,
            "dropped_unknown_probed": 0,
            "dropped_by_source": {},
        }
        for c in candidates_to_download:
            stats["downloaded_by_source"].setdefault(c.source, 0)
            stats["failed_by_source"].setdefault(c.source, 0)

        downloaded_count = 0
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_map = {}
            for cand in candidates_to_download:
                if len(future_map) >= args.target * 1.5:
                    break
                folder_name = cand.post_id if cand.post_id.startswith(f"{cand.source}_") else f"{cand.source}_{cand.post_id}"
                dest = raw_date_dir / folder_name
                fut = executor.submit(downloader.process_candidate, cand, dest)
                future_map[fut] = cand

            for fut in as_completed(future_map):
                cand = future_map[fut]
                try:
                    success, msg, meta = fut.result()
                    if success:
                        downloaded_count += 1
                        stats["downloaded_by_source"][cand.source] += 1
                        logger.success(f"[{downloaded_count}/{args.target}] Downloaded: [{cand.source}] '{cand.title[:50]}'")
                        if downloaded_count >= args.target:
                            logger.info(f"Reached target of {args.target} downloaded videos! Cancelling remaining tasks...")
                            for f in future_map:
                                if not f.done():
                                    f.cancel()
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                    else:
                        if "Duplicate detected" in msg:
                            stats["media_duplicates"] += 1
                        elif "Dropped before download" in msg or "duration_too_" in msg:
                            pass
                        else:
                            stats["failed_by_source"][cand.source] += 1
                        if "Dropped before download" in msg or "Remote probe dropped" in msg:
                            src_drop = stats["dropped_by_source"].setdefault(cand.source, {"too_long": 0, "too_short": 0, "unknown_probed": 0})
                            if "(probed)" in msg:
                                stats["dropped_unknown_probed"] += 1
                                src_drop["unknown_probed"] += 1
                            if "duration_too_long" in msg:
                                stats["dropped_too_long"] += 1
                                src_drop["too_long"] += 1
                            elif "duration_too_short" in msg:
                                stats["dropped_too_short"] += 1
                                src_drop["too_short"] += 1
                            stats["filtered"][msg] = stats["filtered"].get(msg, 0) + 1
                        logger.warning(f"Skipped candidate [{cand.source}] '{cand.title[:40]}': {msg}")
                except Exception as exc:
                    stats["failed_by_source"][cand.source] += 1
                    logger.error(f"Download worker exception for {cand.post_id}: {exc}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        _print_summary(stats, target=args.target, dry_run=False, output_dir=raw_date_dir)
        return

    # --- MODES 2 & 3: LIST-ONLY, DRY-RUN, or FULL RUN ---
    source_names = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    active_sources = []
    for s_name in source_names:
        if s_name in SOURCES:
            source_cfg = config.get(s_name, {})
            if isinstance(source_cfg, dict) and not source_cfg.get("enabled", True):
                logger.info(f"Source '{s_name}' is disabled in config (enabled: false). Skipping.")
                continue
            active_sources.append(get_source_plugin(s_name, config))
        else:
            logger.warning(f"Skipping unrecognized source '{s_name}'. Available: {list(SOURCES.keys())}")

    if not active_sources:
        logger.error("No valid sources configured. Exiting.")
        sys.exit(1)

    stats = {
        "found_by_source": {s.name: 0 for s in active_sources},
        "filtered": {},
        "id_duplicates": 0,
        "media_duplicates": 0,
        "downloaded_by_source": {s.name: 0 for s in active_sources},
        "failed_by_source": {s.name: 0 for s in active_sources},
        "dropped_too_long": 0,
        "dropped_too_short": 0,
        "dropped_unknown_probed": 0,
        "dropped_by_source": {s.name: {"too_long": 0, "too_short": 0, "unknown_probed": 0} for s in active_sources},
    }

    # 1. Fetch Candidates from all sources
    all_raw_candidates = []
    for src in active_sources:
        try:
            logger.info(f"Scraping candidates from [{src.name}]...")
            fetch_target = int(args.target * 1.5 // len(active_sources)) + 10
            cands = src.fetch_candidates(fetch_target)
            stats["found_by_source"][src.name] = len(cands)
            all_raw_candidates.extend(cands)
            logger.info(f"[{src.name}] produced {len(cands)} raw video candidates.")
        except Exception as e:
            logger.error(f"Source [{src.name}] encountered an unhandled error: {e}. Continuing with remaining sources.")

    logger.info(f"Total raw candidates found across all sources: {len(all_raw_candidates)}")

    # Save structured scrape result summary
    reddit_src = next((s for s in active_sources if s.name == "reddit"), None)
    scrape_meta = getattr(reddit_src, "last_result", {}) if reddit_src else {}
    if not scrape_meta:
        scrape_meta = {
            "status": "success" if all_raw_candidates else "partial",
            "requests_used": 0,
            "count_403": 0,
            "count_429": 0,
            "blocked_until": None,
        }
    summary_path = data_dir / "candidates" / f"{date_str}_scrape_result.json"
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as f:
            meta_to_save = {k: v for k, v in scrape_meta.items() if k != "candidates"}
            meta_to_save["raw_candidates_found"] = len(all_raw_candidates)
            json.dump(meta_to_save, f, indent=2)
    except Exception as e:
        logger.debug(f"Failed to write scrape_result.json: {e}")

    # 2. Pre-Download Filtering & ID Deduplication
    valid_candidates = []
    for cand in all_raw_candidates:
        is_seen, dup_msg = dedup.is_id_or_url_seen(cand.post_id, cand.post_url)
        if is_seen:
            stats["id_duplicates"] += 1
            continue

        passed, reason = check_candidate_pre_download(cand, config)
        if not passed:
            stats["filtered"][reason] = stats["filtered"].get(reason, 0) + 1
            src_drop = stats["dropped_by_source"].setdefault(cand.source, {"too_long": 0, "too_short": 0, "unknown_probed": 0})
            if "duration_too_long" in reason:
                stats["dropped_too_long"] += 1
                src_drop["too_long"] += 1
            elif "duration_too_short" in reason:
                stats["dropped_too_short"] += 1
                src_drop["too_short"] += 1

            db.record_seen(
                post_id=cand.post_id,
                source=cand.source,
                post_url=cand.post_url,
                title=cand.title,
                uploader=cand.uploader,
                status="filtered",
                filter_reason=reason,
            )
            continue

        valid_candidates.append(cand)

    logger.info(f"Candidates passing pre-download filter & ID deduplication: {len(valid_candidates)}")

    # 3. Engagement Ranking
    ranked_candidates = rank_candidates(valid_candidates, config)

    # Mode: LIST-ONLY
    if args.list_only:
        out_path = Path(args.candidates_output) if args.candidates_output else (data_dir / "candidates" / f"{date_str}_candidates.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = [c.model_dump() for c in ranked_candidates]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=2)
        logger.info(f"LIST-ONLY: Saved {len(serialized)} ranked candidates to {out_path}")
        _print_summary(stats, target=args.target, dry_run=True, output_dir=raw_date_dir)
        return

    # Mode: DRY RUN
    if args.dry_run:
        logger.info("\n--- DRY RUN SUMMARY (No media files downloaded) ---")
        for i, c in enumerate(ranked_candidates[:args.target], start=1):
            logger.info(f"#{i:02d} [{c.source}] ({c.score} upvotes) {c.title[:65]}...")
        _print_summary(stats, target=args.target, dry_run=True, output_dir=raw_date_dir)
        return

    # Mode: FULL RUN (Download directly)
    workers = args.workers or config.get("general", {}).get("parallel_downloads", 4)
    logger.info(f"Starting parallel downloader with {workers} workers (target: {args.target})...")

    downloaded_count = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_map = {}
        for cand in ranked_candidates:
            if len(future_map) >= args.target * 1.5:
                break
            folder_name = cand.post_id if cand.post_id.startswith(f"{cand.source}_") else f"{cand.source}_{cand.post_id}"
            dest = raw_date_dir / folder_name
            fut = executor.submit(downloader.process_candidate, cand, dest)
            future_map[fut] = cand

        for fut in as_completed(future_map):
            cand = future_map[fut]
            try:
                success, msg, meta = fut.result()
                if success:
                    downloaded_count += 1
                    stats["downloaded_by_source"][cand.source] += 1
                    logger.success(f"[{downloaded_count}/{args.target}] Downloaded: [{cand.source}] '{cand.title[:50]}'")
                    if downloaded_count >= args.target:
                        logger.info(f"Reached target of {args.target} downloaded videos! Cancelling remaining tasks...")
                        for f in future_map:
                            if not f.done():
                                f.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                else:
                    if "Duplicate detected" in msg:
                        stats["media_duplicates"] += 1
                    elif "Dropped before download" in msg or "duration_too_" in msg:
                        pass
                    else:
                        stats["failed_by_source"][cand.source] += 1
                    if "Dropped before download" in msg or "Remote probe dropped" in msg:
                        src_drop = stats["dropped_by_source"].setdefault(cand.source, {"too_long": 0, "too_short": 0, "unknown_probed": 0})
                        if "(probed)" in msg:
                            stats["dropped_unknown_probed"] += 1
                            src_drop["unknown_probed"] += 1
                        if "duration_too_long" in msg:
                            stats["dropped_too_long"] += 1
                            src_drop["too_long"] += 1
                        elif "duration_too_short" in msg:
                            stats["dropped_too_short"] += 1
                            src_drop["too_short"] += 1
                        stats["filtered"][msg] = stats["filtered"].get(msg, 0) + 1
                    logger.warning(f"Skipped candidate [{cand.source}] '{cand.title[:40]}': {msg}")
            except Exception as exc:
                stats["failed_by_source"][cand.source] += 1
                logger.error(f"Download worker exception for {cand.post_id}: {exc}")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    _print_summary(stats, target=args.target, dry_run=False, output_dir=raw_date_dir)


def _print_summary(stats: Dict[str, Any], target: int, dry_run: bool, output_dir: Path) -> None:
    """Print clean formatted terminal summary table."""
    total_found = sum(stats["found_by_source"].values())
    total_downloaded = sum(stats["downloaded_by_source"].values())
    total_failed = sum(stats["failed_by_source"].values())
    total_filtered = sum(stats["filtered"].values())

    print("\n" + "=" * 65)
    print("                     SCRAPING RUN SUMMARY")
    print("=" * 65)
    print(f"  Target Goal               : {target} videos")
    print(f"  Total Candidates Found    : {total_found}")
    for src, count in stats["found_by_source"].items():
        print(f"    - {src.capitalize():<12}           : {count}")

    too_long = stats.get("dropped_too_long", 0)
    too_short = stats.get("dropped_too_short", 0)
    unknown_probed = stats.get("dropped_unknown_probed", 0)
    drop_summary_line = f"dropped before download: {too_long} too long, {too_short} too short, {unknown_probed} unknown duration (probed)"

    print(f"\n  Pre-Download Filtered Out : {total_filtered}")
    print(f"  {drop_summary_line}")
    logger.info(drop_summary_line)
    for reason, count in stats["filtered"].items():
        print(f"    - {reason:<22}  : {count}")
    
    dropped_by_src = stats.get("dropped_by_source", {})
    if any(sum(d.values()) > 0 for d in dropped_by_src.values()):
        print(f"\n  Dropped Duration Counts Per Source:")
        for src, counts in dropped_by_src.items():
            if sum(counts.values()) > 0:
                print(f"    - {src.capitalize():<12} : {counts['too_long']} too long, {counts['too_short']} too short, {counts['unknown_probed']} probed")

    print(f"\n  Duplicates Blocked        :")
    print(f"    - ID / URL Duplicates   : {stats['id_duplicates']}")
    print(f"    - Media Hash/pHash Reposts: {stats['media_duplicates']}")

    if not dry_run:
        print(f"\n  Successfully Downloaded   : {total_downloaded}")
        for src, count in stats["downloaded_by_source"].items():
            print(f"    - {src.capitalize():<12}           : {count}")
        print(f"  Download Failures         : {total_failed}")
        print(f"\n  📁 Destination Folder     : {output_dir.resolve()}")
    else:
        print(f"\n  Mode                      : LIST-ONLY / DRY RUN (no media files downloaded)")

    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
