import os
import sys
from typing import Dict, Any, List, Optional
from loguru import logger
from graph.state import CurationState
from llm.schemas import PlanResult
from llm.prompts import build_plan_system_prompt, build_plan_user_prompt


def plan_node(
    state: CurationState,
    config: Dict[str, Any],
    llm_client: Any,
    db: Optional[Any] = None,
    gateway: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Decide where to discover videos today.
    Gentle Scraper & Budget Aware:
      - Respects config's allowed listings (default: hot, top_day, top_week; never heavy lists like top_month/top_year).
      - Checks RedditGateway request budget and circuit breaker.
      - If budget is exhausted or breaker is open, bypasses Reddit scraping and relies on reserve pool / backfill.
      - Uses Source Registry with ~80% active / ~20% trial bandit selection respecting allowed listings.
      - Loop 1 picks top sources from registry within run budget.
      - Loop 2+ adapts by selecting deeper pages & gentle archive windows (top_week/top_day) without touching heavy lists.
    """
    loop_count = state.get("loop_count", 0) + 1
    target_count = state.get("target_count", 6)
    cfg_buffer = state.get("spare_buffer", 2)
    buffer = min(cfg_buffer, target_count // 2) if target_count >= 2 else 0

    # Reserve pool stock check: if reserve is below target_stock, expand curation goal
    reserve_cfg = config.get("reserve_pool", {})
    target_stock = int(reserve_cfg.get("target_stock", 12))
    stock_buffer = int(reserve_cfg.get("stock_buffer_per_run", 2))
    reserve_deficit = 0
    if db and hasattr(db, "fetch_reserve_pool_candidates"):
        try:
            current_reserve_count = len(db.fetch_reserve_pool_candidates(limit=50))
            if current_reserve_count < target_stock:
                reserve_deficit = min(target_stock - current_reserve_count, stock_buffer)
                logger.info(
                    f"Reserve pool stock check: {current_reserve_count}/{target_stock} available. "
                    f"Adding +{reserve_deficit} reserve deficit to curation goal."
                )
        except Exception as e:
            logger.debug(f"Could not check reserve count: {e}")

    target_needed = max(
        1,
        (target_count + buffer + reserve_deficit)
        - len(state.get("approved", []))
        - len(state.get("borderline", []))
    )

    allowed_sources = config.get("allowed_sources", {})
    allowed_subs = allowed_sources.get("subreddits", ["funny", "Unexpected", "MadeMeSmile", "AnimalsBeingDerps"])
    twitter_enabled = allowed_sources.get("twitter_enabled", False)
    allowed_twit = allowed_sources.get("twitter_accounts", ["DailyViralVids", "LaughsDaily"]) if twitter_enabled else []
    sources_tried = list(state.get("sources_tried", []))

    # Reddit gentle config & budget settings
    reddit_cfg = config.get("reddit", {})
    allowed_listings: List[str] = (
        reddit_cfg.get("listings")
        or allowed_sources.get("listings")
        or ["hot", "top_day", "top_week"]
    )
    max_requests_per_run = int(reddit_cfg.get("max_requests_per_run", 30))
    max_requests_per_day = int(reddit_cfg.get("max_requests_per_day", 80))
    subreddits_per_run = int(reddit_cfg.get("subreddits_per_run", 4))

    # Inspect RedditGateway budget & circuit breaker
    if gateway is None:
        gateway = config.get("gateway")

    is_testing = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING")
        or "pytest" in sys.modules
    )

    status = {}
    if gateway is None and not is_testing:
        try:
            from tools.scraper_tool import ScraperTool
            st = ScraperTool()
            status = st.get_reddit_status()
        except Exception as e:
            logger.debug(f"Could not get reddit status from ScraperTool: {e}")
            status = {}
    elif gateway is None and is_testing and "reddit_status" in config:
        status = config["reddit_status"]

    remaining_budget = max_requests_per_run
    is_breaker_open = False
    breaker_reason = ""

    if status and "mode" in status:
        requests_today = status.get("requests_today", 0)
        max_requests_per_day = status.get("max_per_day", max_requests_per_day)
        max_requests_per_run = status.get("requests_this_run_budget", max_requests_per_run)
        allowed_listings = status.get("allowed_listings") or allowed_listings

        breaker = status.get("breaker", {})
        open_layers = [l for l, info in breaker.items() if info.get("is_open")]
        is_breaker_open = bool(open_layers) or status.get("any_layer_open", False)
        all_layers_open = status.get("all_layers_open", False)

        breaker_reason = ", ".join(f"{l}: {breaker[l].get('reason')}" for l in open_layers) if open_layers else ""
        day_remaining = max(0, max_requests_per_day - requests_today)
        remaining_budget = min(max_requests_per_run, day_remaining)

        logger.info(
            f"--- [Node: PLAN] Loop #{loop_count} | Target needed: {target_needed} clips | "
            f"Reddit budget: {remaining_budget} remaining (today: {requests_today}/{max_requests_per_day}, run max: {max_requests_per_run}) | "
            f"Breaker open: {is_breaker_open} (open layers: {open_layers or 'none'}) | "
            f"Allowed listings: {allowed_listings} ---"
        )
    elif gateway:
        is_breaker_open, breaker_reason = gateway.check_circuit_breaker(layer="json")
        if not is_breaker_open:
            is_breaker_open, breaker_reason = gateway.check_circuit_breaker(layer="rss")

        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
            today_used = gateway.db.get_requests_count_today(today_ist, host="reddit")
            run_remaining = max(0, gateway.max_requests_per_run - gateway.requests_this_run)
            day_remaining = max(0, gateway.max_requests_per_day - today_used)
            remaining_budget = min(run_remaining, day_remaining)
        except Exception:
            remaining_budget = max(0, max_requests_per_run - getattr(gateway, "requests_this_run", 0))

        logger.info(
            f"--- [Node: PLAN] Loop #{loop_count} | Target needed: {target_needed} clips | "
            f"Reddit budget: {remaining_budget} remaining (run: {getattr(gateway, 'requests_this_run', 0)}/{max_requests_per_run}) | "
            f"Breaker open: {is_breaker_open} | Allowed listings: {allowed_listings} ---"
        )
    else:
        logger.info(
            f"--- [Node: PLAN] Loop #{loop_count} | Target needed: {target_needed} clips | "
            f"Reddit budget: {remaining_budget} remaining (no gateway info) | "
            f"Breaker open: {is_breaker_open} | Allowed listings: {allowed_listings} ---"
        )

    # 1. Budget & Circuit Breaker Guard: Skip Reddit scrape if budget exhausted or breaker is open
    if is_breaker_open or remaining_budget <= 0:
        reason = (
            f"circuit breaker is open ({breaker_reason})"
            if is_breaker_open
            else f"request budget is exhausted ({remaining_budget} remaining)"
        )
        logger.warning(f"[Node: PLAN] Reddit scraping skipped because {reason}. Relying on reserve pool & backfill.")
        plan = {
            "subreddits": [],
            "twitter_accounts": [],
            "strategy_reason": f"Reddit {reason}; relying on reserve pool and backfill.",
            "target_to_fetch": 0,
            "proposed_new_sources": [],
        }
        return {
            "plan": plan,
            "selected_sources": [],
            "sources_tried": sources_tried,
            "loop_count": loop_count,
            "budget_exhausted": True,
            "trigger_extra_loop": False,
        }

    # 2. Request Budget Scaling: Each subreddit uses ~1-2 requests (hot, top_day, or top_week).
    # Cap planned subreddits to prevent over-budget requests.
    max_subs = max(1, min(subreddits_per_run, remaining_budget // 2)) if remaining_budget > 1 else 1

    # If database is provided, query bandit selection respecting allowed listings
    selected_from_registry: List[Dict[str, Any]] = []
    if db and hasattr(db, "get_sources_for_selection"):
        try:
            selected_from_registry = db.get_sources_for_selection(
                total_budget=max_subs,
                active_ratio=0.8,
                trial_ratio=0.2,
                allowed_listings=allowed_listings,
            )
            reg_subs = [s["name"] for s in selected_from_registry if s["source_type"] == "subreddit"]
            if reg_subs:
                allowed_subs = reg_subs
        except Exception as e:
            logger.warning(f"Error querying sources from registry: {e}")

    # 3. Loop 1 Plan: Select up to max_subs using registry bandit selection on gentle listings
    if loop_count == 1:
        initial_subs = allowed_subs[:max_subs]
        initial_twit = []  # X is experimental; test Reddit-only first
        plan = {
            "subreddits": initial_subs,
            "twitter_accounts": initial_twit,
            "strategy_reason": f"80/20 Registry selection ({len(initial_subs)} subs, budget: {remaining_budget} reqs).",
            "target_to_fetch": target_needed if target_needed == 1 else int(target_needed * 1.5),
            "proposed_new_sources": [],
        }
        for s in initial_subs:
            if s not in sources_tried:
                sources_tried.append(s)
        for t in initial_twit:
            if t not in sources_tried:
                sources_tried.append(t)

        return {
            "plan": plan,
            "selected_sources": selected_from_registry[:max_subs] if selected_from_registry else [],
            "sources_tried": sources_tried,
            "loop_count": loop_count,
            "trigger_extra_loop": False,
        }

    # 4. Loops 2 and 3: Move to deeper pages & gentle allowed archival time windows (NEVER heavy lists)
    if loop_count in (2, 3) and db and hasattr(db, "get_deeper_archive_sources"):
        deeper_sources = db.get_deeper_archive_sources(
            loop_count=loop_count,
            limit=max_subs,
            allowed_listings=allowed_listings,
            exclude_names=sources_tried,
        )
        if deeper_sources:
            deeper_subs = [s["name"] for s in deeper_sources if s.get("source_type") == "subreddit"]
            source_labels = [f"{s.get('name')}/{s.get('listing_mode')}" for s in deeper_sources]
            logger.info(
                f"[Node: PLAN] Loop #{loop_count}: Pivoting to gentle allowed archive windows ({source_labels})"
            )
            plan = {
                "subreddits": deeper_subs,
                "twitter_accounts": [],
                "strategy_reason": f"Gentle archive windows & pagination (Loop #{loop_count}) respecting config listings.",
                "target_to_fetch": min(target_needed * 2, max_subs * 10),
                "proposed_new_sources": [],
            }
            for s in deeper_subs:
                if s not in sources_tried:
                    sources_tried.append(s)

            return {
                "plan": plan,
                "selected_sources": deeper_sources,
                "sources_tried": sources_tried,
                "loop_count": loop_count,
                "trigger_extra_loop": False,
            }

    # 5. Loop 2+: LLM-guided adaptation fallback (if deeper registry sources not available)
    rejections = state.get("rejected", [])
    reject_counts: Dict[str, int] = {}
    for r in rejections:
        reason = r.get("reason", "unknown")
        reject_counts[reason] = reject_counts.get(reason, 0) + 1

    sys_prompt = build_plan_system_prompt(allowed_subs, allowed_twit)
    usr_prompt = build_plan_user_prompt(loop_count, target_needed, reject_counts, sources_tried)

    try:
        plan_res: PlanResult = llm_client.call_structured(
            task_type="plan",
            system_prompt=sys_prompt,
            user_prompt=usr_prompt,
            schema=PlanResult,
        )
        # Validate that selected sources are in allowed list
        safe_subs = [s for s in plan_res.subreddits if s in allowed_subs][:max_subs]
        safe_twit = [t for t in plan_res.twitter_accounts if t in allowed_twit]

        # If LLM selected nothing valid from allowed list, pick untried allowed sources
        if not safe_subs:
            untried = [s for s in allowed_subs if s not in sources_tried]
            safe_subs = untried[:max_subs] if untried else allowed_subs[:max_subs]

        plan = {
            "subreddits": safe_subs,
            "twitter_accounts": safe_twit,
            "strategy_reason": plan_res.strategy_reason,
            "target_to_fetch": min(target_needed * 2, max_subs * 10),
            "proposed_new_sources": plan_res.proposed_new_sources,
        }
        for s in safe_subs + safe_twit:
            if s not in sources_tried:
                sources_tried.append(s)

        # Ensure selected_sources uses allowed listings only
        default_listing = allowed_listings[0] if allowed_listings else "hot"
        selected_custom = [
            {"source_type": "subreddit", "name": s, "listing_mode": default_listing}
            for s in safe_subs
        ]

        logger.info(f"Adaptive plan created: {safe_subs} + {safe_twit} ({plan_res.strategy_reason})")
        return {
            "plan": plan,
            "selected_sources": selected_custom,
            "sources_tried": sources_tried,
            "loop_count": loop_count,
            "trigger_extra_loop": False,
        }

    except Exception as e:
        logger.error(f"Plan LLM failed: {e}. Falling back to untried allowed sources.")
        untried = [s for s in allowed_subs if s not in sources_tried]
        fallback_subs = untried[:max_subs] if untried else allowed_subs[:max_subs]
        default_listing = allowed_listings[0] if allowed_listings else "hot"
        selected_custom = [
            {"source_type": "subreddit", "name": s, "listing_mode": default_listing}
            for s in fallback_subs
        ]
        return {
            "plan": {
                "subreddits": fallback_subs,
                "twitter_accounts": allowed_twit[:1] if allowed_twit else [],
                "strategy_reason": "Fallback to untried allowed sources.",
                "target_to_fetch": min(target_needed * 2, max_subs * 10),
                "proposed_new_sources": [],
            },
            "selected_sources": selected_custom,
            "sources_tried": sources_tried + fallback_subs,
            "loop_count": loop_count,
            "trigger_extra_loop": False,
        }
