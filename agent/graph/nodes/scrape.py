import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger
from graph.state import CurationState
from tools.scraper_tool import ScraperTool


def scrape_list_node(state: CurationState, scraper_tool: ScraperTool) -> Dict[str, Any]:
    """
    Step 1 of two-step scraper:
    Discovers, filters, deduplicates and ranks candidates in --list-only mode.
    Outputs metadata JSON without downloading media files.
    """
    target_date = state.get("target_date", "tomorrow")
    plan = state.get("plan", {})
    target_to_fetch = plan.get("target_to_fetch", 20)
    dry_run = state.get("dry_run", False)

    sources = []
    if plan.get("subreddits") or state.get("selected_sources"):
        sources.append("reddit")
    if plan.get("twitter_accounts"):
        sources.append("twitter")

    logger.info(f"--- [Node: SCRAPE_LIST] Listing candidates for {target_date} ---")

    subreddits = plan.get("subreddits", [])
    custom_sources = state.get("selected_sources", [])

    raw_candidates = scraper_tool.list_candidates(
        target_count=target_to_fetch * 2,
        date_str=target_date,
        sources=sources,
        subreddits=subreddits,
        custom_sources=custom_sources,
    )

    logger.info(f"Scrape-list found {len(raw_candidates)} pre-download candidates.")
    res = {
        "raw_candidates": raw_candidates,
    }
    if hasattr(scraper_tool, "is_scraper_blocked") and scraper_tool.is_scraper_blocked():
        logger.warning("Scraper reported circuit breaker blocked state.")
        res["scraper_blocked"] = True
    elif (hasattr(scraper_tool, "has_scraper_error") and scraper_tool.has_scraper_error()) or getattr(scraper_tool, "last_scrape_status", None) == "error":
        logger.error("Scraper reported non-zero crash/error state. Recording scraper_error.")
        res["scraper_error"] = True
    return res


def download_node(
    state: CurationState,
    scraper_tool: ScraperTool,
    db: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Step 2 of two-step scraper:
    Downloads media only for surviving candidates that passed the title pre-screen.
    Also injects pending retry candidates from previous judge failures.
    """
    target_date = state.get("target_date", "tomorrow")
    plan = state.get("plan", {})
    target_to_fetch = plan.get("target_to_fetch", 20)
    vetted_candidates = state.get("vetted_candidates") or state.get("raw_candidates", [])

    logger.info(f"--- [Node: DOWNLOAD] Downloading {len(vetted_candidates)} vetted candidates ---")

    if not state.get("dry_run", False):
        scraper_tool.download_candidates(
            candidates=vetted_candidates,
            target_count=target_to_fetch,
            date_str=target_date,
        )

    # Collect downloaded clips with their paths & meta.json
    downloaded_clips = scraper_tool.get_downloaded_videos(target_date)

    # Filter out already seen
    existing_seen = {c.get("post_id") for c in state.get("candidates", []) if c.get("post_id")}
    existing_hashes = {c.get("file_hash") for c in state.get("candidates", []) if c.get("file_hash")}
    for category in ("approved", "borderline", "rejected"):
        for item in state.get(category, []):
            if item.get("post_id"):
                existing_seen.add(item.get("post_id"))
            if item.get("file_hash"):
                existing_hashes.add(item.get("file_hash"))

    final_candidates = []
    for c in downloaded_clips:
        p_id = c.get("post_id")
        f_hash = c.get("file_hash")
        if (p_id and p_id in existing_seen) or (f_hash and f_hash in existing_hashes):
            continue
        final_candidates.append(c)
        if p_id:
            existing_seen.add(p_id)

    # Inject pending retry candidates from previous judge failures
    if db is not None:
        try:
            pending_retries = db.pop_pending_judge_retries()
            if pending_retries:
                logger.info(f"Injecting {len(pending_retries)} candidate(s) for retry from previous judge failure(s).")
                for pr in pending_retries:
                    p_id = pr.get("post_id")
                    if p_id and p_id not in existing_seen:
                        final_candidates.append(pr)
                        existing_seen.add(p_id)
        except Exception as e:
            logger.debug(f"Could not load pending judge retries: {e}")

    # If dry-run mode and no media downloaded, use vetted metadata dicts directly
    if state.get("dry_run", False) and not final_candidates:
        final_candidates = vetted_candidates[:target_to_fetch]

    logger.info(f"Download node finalized {len(final_candidates)} clips ready for evaluation.")
    res = {
        "candidates": final_candidates,
    }
    if hasattr(scraper_tool, "has_scraper_error") and scraper_tool.has_scraper_error():
        res["scraper_error"] = True
    return res


def scrape_node(state: CurationState, scraper_tool: ScraperTool) -> Dict[str, Any]:
    """
    Composite scraper node (retains full backwards compatibility for tests).
    Combines scrape_list and download if executed directly.
    """
    list_res = scrape_list_node(state, scraper_tool)
    temp_state = dict(state)
    temp_state["raw_candidates"] = list_res.get("raw_candidates", [])
    temp_state["vetted_candidates"] = list_res.get("raw_candidates", [])
    if list_res.get("scraper_blocked"):
        temp_state["scraper_blocked"] = True
    if list_res.get("scraper_error"):
        temp_state["scraper_error"] = True
    down_res = download_node(temp_state, scraper_tool)
    if list_res.get("scraper_blocked"):
        down_res["scraper_blocked"] = True
    if list_res.get("scraper_error"):
        down_res["scraper_error"] = True
    return down_res
