from typing import Dict, Any, List
from loguru import logger

from graph.state import CurationState
from core.db import AgentDatabase
from core.context_collector import collect_fresh_context
from core.source_verifier import verify_sources
from llm.client import LLMClient
from llm.schemas import DiscoveryResult
from llm.prompts import (
    build_discovery_system_prompt,
    build_discovery_user_prompt,
)


def discover_node(
    state: CurationState,
    config: Dict[str, Any],
    llm_client: LLMClient,
    db: AgentDatabase,
) -> Dict[str, Any]:
    """
    Phase 3.5: Dynamic Discovery Node.
    Runs ONCE per day (Loop 1).
    1. Collects fresh context (trending titles + registry metrics, zero LLM).
    2. Calls discovery LLM (1 call) to suggest candidate subreddits and search queries.
    3. Verifies suggestions via plain-code verifier (NSFW, video posts, daily cap of 5).
    4. Adds verified trial sources to the Source Registry.
    5. Sets state['discovered'] = True so subsequent loops skip this node.
    """
    if state.get("discovered", False):
        logger.info("Discovery already completed for today; skipping discover node.")
        return {}

    logger.info("--- [Node: DISCOVER] Running Daily LLM Discovery ---")
    target_date = state.get("target_date", "")

    # Shared RedditGateway for budget and circuit breaker checks
    try:
        from scraper.core.reddit_gateway import RedditGateway
        gateway = RedditGateway()
    except Exception:
        gateway = None

    # 1. Fresh Context Collection (deterministic plain code)
    fresh_context = collect_fresh_context(db, config, gateway=gateway)
    fresh_titles = fresh_context.get("fresh_titles", [])
    active_sources = fresh_context.get("active_sources", [])
    top_reject_reasons = fresh_context.get("top_reject_reasons", [])
    already_tried = fresh_context.get("already_tried_names", [])

    # 2. Discovery LLM Call
    reddit_cfg = config.get("reddit", {})
    allowed_listings: List[str] = (
        reddit_cfg.get("listings")
        or config.get("allowed_sources", {}).get("listings")
        or ["hot", "top_day", "top_week"]
    )
    sys_prompt = build_discovery_system_prompt(allowed_listings=allowed_listings)
    usr_prompt = build_discovery_user_prompt(
        fresh_titles=fresh_titles,
        active_sources=active_sources,
        top_reject_reasons=top_reject_reasons,
        already_tried_names=already_tried,
    )

    suggested_candidates: List[Dict[str, Any]] = []
    try:
        res: DiscoveryResult = llm_client.generate_structured(
            system_prompt=sys_prompt,
            user_prompt=usr_prompt,
            response_schema=DiscoveryResult,
            task_type="discover",
        )

        for sub in res.subreddits:
            sub_mode = sub.listing_mode if sub.listing_mode in allowed_listings else allowed_listings[0]
            suggested_candidates.append({
                "source_type": "subreddit",
                "name": sub.name,
                "listing_mode": sub_mode,
                "reason": sub.reason,
            })
        for q in res.search_queries:
            q_mode = q.listing_mode if q.listing_mode in allowed_listings else allowed_listings[0]
            suggested_candidates.append({
                "source_type": "search_query",
                "name": q.name,
                "listing_mode": q_mode,
                "reason": q.reason,
            })

        logger.info(
            f"Discovery LLM proposed {len(res.subreddits)} subreddits and {len(res.search_queries)} search queries."
        )

    except Exception as e:
        logger.warning(f"Discovery LLM call encountered error: {e}. Falling back to existing registry.")
        return {"discovered": True}

    # 3. Source Verification (plain code, NSFW/existence check, cap 5/day)
    verified, rejected = verify_sources(
        suggested_sources=suggested_candidates,
        db=db,
        max_daily_trials=5,
        skip_online_check=state.get("dry_run", False),
        gateway=gateway,
    )

    # 4. Add verified trial sources to registry
    added = db.add_trial_sources(verified, run_date=target_date, max_daily_trials=5)
    logger.info(f"Added {added} new verified trial sources to Source Registry.")

    # 5. Log discovery outcome
    db.log_discovery(
        run_date=target_date,
        suggested=suggested_candidates,
        verified=verified,
        rejected=rejected,
    )

    return {
        "discovered": True,
        "cost_so_far": llm_client.total_cost_usd,
        "llm_calls_count": llm_client.call_count,
    }
