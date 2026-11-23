import sqlite3
from pathlib import Path
from typing import Dict, Any, Literal
from loguru import logger

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from graph.state import CurationState
from graph.nodes import (
    discover_node,
    plan_node,
    scrape_list_node,
    title_prescreen_node,
    download_node,
    cheap_filters_node,
    judge_node,
    decide_node,
    rights_check_node,
    human_review_node,
    backfill_node,
    update_registry_node,
    caption_node,
    edit_node,
    schedule_node,
)


def build_curation_graph(
    config: Dict[str, Any],
    llm_client: Any,
    scraper_tool: Any,
    editor_tool: Any,
    db: Any,
    checkpointer_conn: sqlite3.Connection,
):
    """
    Constructs the Phase 3.5 LangGraph state graph with:
      - Daily Discovery LLM Node (Loop 1 only)
      - Source Registry 80/20 Bandit Selection
      - Two-step Scraper: --list-only -> Title Pre-Screen -> --download-only
      - Parallel Calibrated Vision Judge & Comparative Borderline Ranker
      - Post-Decision Registry Metric Updates (after human review & backfill)
      - SQLite Checkpointing
    """
    workflow = StateGraph(CurationState)

    # 1. Define Node Wrappers
    def _discover(state: CurationState) -> Dict[str, Any]:
        return discover_node(state, config, llm_client, db)

    def _plan(state: CurationState) -> Dict[str, Any]:
        return plan_node(state, config, llm_client, db)

    def _scrape_list(state: CurationState) -> Dict[str, Any]:
        return scrape_list_node(state, scraper_tool)

    def _title_prescreen(state: CurationState) -> Dict[str, Any]:
        return title_prescreen_node(state, config, llm_client)

    def _download(state: CurationState) -> Dict[str, Any]:
        return download_node(state, scraper_tool, db)

    def _cheap_filters(state: CurationState) -> Dict[str, Any]:
        return cheap_filters_node(state, config)

    def _judge(state: CurationState) -> Dict[str, Any]:
        return judge_node(state, config, llm_client, db)

    def _decide(state: CurationState) -> Dict[str, Any]:
        return decide_node(state, config, db)

    def _rights_check(state: CurationState) -> Dict[str, Any]:
        return rights_check_node(state, config, db)

    def _human_review(state: CurationState) -> Dict[str, Any]:
        return human_review_node(state, db, config=config)

    def _backfill(state: CurationState) -> Dict[str, Any]:
        return backfill_node(state, db)

    def _update_registry(state: CurationState) -> Dict[str, Any]:
        return update_registry_node(state, db)

    def _caption(state: CurationState) -> Dict[str, Any]:
        return caption_node(state, llm_client)

    def _edit(state: CurationState) -> Dict[str, Any]:
        return edit_node(state, editor_tool)

    def _schedule(state: CurationState) -> Dict[str, Any]:
        return schedule_node(state, config, db)

    # Add Nodes
    workflow.add_node("discover", _discover)
    workflow.add_node("plan", _plan)
    workflow.add_node("scrape_list", _scrape_list)
    workflow.add_node("title_prescreen", _title_prescreen)
    workflow.add_node("download", _download)
    workflow.add_node("cheap_filters", _cheap_filters)
    workflow.add_node("judge", _judge)
    workflow.add_node("decide", _decide)
    workflow.add_node("rights_check", _rights_check)
    workflow.add_node("human_review", _human_review)
    workflow.add_node("backfill", _backfill)
    workflow.add_node("update_registry", _update_registry)
    workflow.add_node("caption", _caption)
    workflow.add_node("edit", _edit)
    workflow.add_node("schedule", _schedule)

    # 2. Linear Pipeline Edges (Start -> Discover -> Plan -> Two-Step Scraper -> Judge -> Decide -> Rights Check)
    workflow.add_edge(START, "discover")
    workflow.add_edge("discover", "plan")
    workflow.add_edge("plan", "scrape_list")
    workflow.add_edge("scrape_list", "title_prescreen")
    workflow.add_edge("title_prescreen", "download")
    workflow.add_edge("download", "cheap_filters")
    workflow.add_edge("cheap_filters", "judge")
    workflow.add_edge("judge", "decide")
    workflow.add_edge("decide", "rights_check")

    # 3. Conditional Routing after 'rights_check' (Count Check)
    def route_after_rights_check(state: CurationState) -> Literal["plan", "human_review", "backfill"]:
        loop_count = state.get("loop_count", 1)
        max_loops = state.get("max_loops", config.get("max_loops", 3))
        target_count = state.get("target_count", 6)
        cfg_buffer = state.get("spare_buffer", 2)
        buffer = min(cfg_buffer, target_count // 2) if target_count >= 2 else 0

        # Reserve deficit buffer
        reserve_cfg = config.get("reserve_pool", {})
        target_stock = int(reserve_cfg.get("target_stock", 12))
        stock_buffer = int(reserve_cfg.get("stock_buffer_per_run", 2))
        reserve_deficit = 0
        if db and hasattr(db, "fetch_reserve_pool_candidates"):
            try:
                res_count = len(db.fetch_reserve_pool_candidates(limit=50))
                if res_count < target_stock:
                    reserve_deficit = min(target_stock - res_count, stock_buffer)
            except Exception:
                pass

        goal = target_count + buffer + reserve_deficit

        total_acquired = len(state.get("approved", [])) + len(state.get("borderline", []))
        logger.info(f"Count check after rights: {total_acquired}/{goal} clips (Loop {loop_count}/{max_loops})")

        # Circuit breaker safety: if scraper is blocked, do not re-plan extra loops
        if state.get("scraper_blocked"):
            logger.warning("Scraper is blocked by circuit breaker. Bypassing replan loops.")
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        # Crash safety: if scraper process crashed, do not re-plan extra loops
        if state.get("scraper_error"):
            logger.error("Scraper process crashed (non-zero exit). Bypassing replan loops.")
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        # Budget safety: if Reddit request budget is exhausted, do not re-plan extra loops
        if state.get("budget_exhausted"):
            logger.warning("Reddit request budget is exhausted. Bypassing replan loops.")
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        # Loop back to PLAN if still under goal and under max_loops (never discover!)
        if total_acquired < goal and loop_count < max_loops and not state.get("backfill_performed", False):
            logger.info(f"Target shortage after rights check ({total_acquired} < {goal}). Looping back to PLAN node.")
            return "plan"

        if len(state.get("borderline", [])) > 0:
            return "human_review"

        return "backfill"

    workflow.add_conditional_edges(
        "rights_check",
        route_after_rights_check,
        {
            "plan": "plan",
            "human_review": "human_review",
            "backfill": "backfill",
        },
    )

    # 4. Human Review proceeds to Backfill
    workflow.add_edge("human_review", "backfill")

    # 5. Conditional Routing after 'backfill'
    def route_after_backfill(state: CurationState) -> Literal["plan", "update_registry"]:
        if state.get("human_interrupted", False):
            return "update_registry"

        if state.get("scraper_blocked") or state.get("scraper_error") or state.get("budget_exhausted"):
            logger.warning("Scraper is blocked, crashed, or budget exhausted. Suppressing extra loop from backfill.")
            return "update_registry"

        if state.get("trigger_extra_loop", False):
            logger.info("Backfill requested extra loop: routing to PLAN node.")
            return "plan"

        return "update_registry"

    workflow.add_conditional_edges(
        "backfill",
        route_after_backfill,
        {
            "plan": "plan",
            "update_registry": "update_registry",
        },
    )

    # 6. Post-Decision Registry Update proceeds to Caption & Export
    workflow.add_edge("update_registry", "caption")
    workflow.add_edge("caption", "edit")
    workflow.add_edge("edit", "schedule")
    workflow.add_edge("schedule", END)

    # 7. Checkpointer
    checkpointer = SqliteSaver(conn=checkpointer_conn)
    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=[],
    )
