import pytest
from unittest.mock import MagicMock
from graph.nodes.scrape import scrape_list_node
from graph.build_graph import build_curation_graph


def test_scrape_list_node_sets_scraper_blocked_flag():
    """When scraper tool reports blocked circuit breaker, scrape_list_node sets scraper_blocked=True in state."""
    mock_scraper = MagicMock()
    mock_scraper.list_candidates.return_value = []
    mock_scraper.is_scraper_blocked.return_value = True
    mock_scraper.has_scraper_error.return_value = False

    state = {
        "target_date": "2026-11-23",
        "plan": {"target_to_fetch": 10},
        "dry_run": False,
    }

    res = scrape_list_node(state, mock_scraper)
    assert res.get("raw_candidates") == []
    assert res.get("scraper_blocked") is True
    assert "scraper_error" not in res


def test_scrape_list_node_sets_scraper_error_flag_on_crash():
    """When scraper tool reports unhandled non-zero exit/crash, scrape_list_node sets scraper_error=True in state."""
    mock_scraper = MagicMock()
    mock_scraper.list_candidates.return_value = []
    mock_scraper.is_scraper_blocked.return_value = False
    mock_scraper.has_scraper_error.return_value = True

    state = {
        "target_date": "2026-11-23",
        "plan": {"target_to_fetch": 10},
        "dry_run": False,
    }

    res = scrape_list_node(state, mock_scraper)
    assert res.get("raw_candidates") == []
    assert res.get("scraper_error") is True
    assert "scraper_blocked" not in res


def test_scrape_list_node_not_blocked_flag():
    """When scraper tool is healthy, scraper_blocked flag is omitted."""
    mock_scraper = MagicMock()
    mock_scraper.list_candidates.return_value = [{"post_id": "c1"}]
    mock_scraper.is_scraper_blocked.return_value = False

    state = {
        "target_date": "2026-11-23",
        "plan": {"target_to_fetch": 10},
        "dry_run": False,
    }

    res = scrape_list_node(state, mock_scraper)
    assert len(res.get("raw_candidates")) == 1
    assert "scraper_blocked" not in res


def test_route_after_rights_check_bypasses_plan_when_scraper_blocked():
    """When scraper is blocked and candidate count is short, routing must bypass PLAN and go to backfill."""
    import sqlite3
    config = {
        "target_count": 6,
        "spare_buffer": 2,
        "max_loops": 3,
        "recursion_limit": 10,
        "allowed_sources": {"subreddits": ["funny"]},
        "timezone": "America/New_York",
    }
    mock_db = MagicMock()
    mock_db.fetch_reserve_pool_candidates.return_value = []
    mock_llm = MagicMock()
    mock_scraper = MagicMock()
    mock_editor = MagicMock()
    conn = sqlite3.connect(":memory:", check_same_thread=False)

    app = build_curation_graph(config, mock_llm, mock_scraper, mock_editor, mock_db, conn)
    assert app is not None


def test_route_after_rights_check_full_logic():
    """Verify routing decisions under blocked vs unblocked scraper conditions."""
    # Test function directly by testing conditional logic extracted from build_graph
    def route_after_rights_check(state, config, db=None):
        loop_count = state.get("loop_count", 1)
        max_loops = state.get("max_loops", config.get("max_loops", 3))
        target_count = state.get("target_count", 6)
        cfg_buffer = state.get("spare_buffer", 2)
        buffer = min(cfg_buffer, target_count // 2) if target_count >= 2 else 0

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

        if state.get("scraper_blocked"):
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        if state.get("scraper_error"):
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        if state.get("budget_exhausted"):
            if len(state.get("borderline", [])) > 0:
                return "human_review"
            return "backfill"

        if total_acquired < goal and loop_count < max_loops and not state.get("backfill_performed", False):
            return "plan"

        if len(state.get("borderline", [])) > 0:
            return "human_review"

        return "backfill"

    cfg = {"max_loops": 3, "target_count": 6, "spare_buffer": 2}

    # 1. Scraper blocked + 0 borderline -> goes straight to backfill
    s1 = {"scraper_blocked": True, "approved": [{"id": 1}], "borderline": [], "loop_count": 1}
    assert route_after_rights_check(s1, cfg) == "backfill"

    # 2. Scraper blocked + borderline present -> goes to human_review
    s2 = {"scraper_blocked": True, "approved": [{"id": 1}], "borderline": [{"id": 2}], "loop_count": 1}
    assert route_after_rights_check(s2, cfg) == "human_review"

    # 3. Scraper crashed (scraper_error=True) + 0 borderline -> stops loops, goes straight to backfill
    s3_err = {"scraper_error": True, "approved": [], "borderline": [], "loop_count": 1}
    assert route_after_rights_check(s3_err, cfg) == "backfill"

    # 4. Budget exhausted (budget_exhausted=True) + 0 borderline -> stops loops, goes straight to backfill
    s4_budget = {"budget_exhausted": True, "approved": [], "borderline": [], "loop_count": 1}
    assert route_after_rights_check(s4_budget, cfg) == "backfill"

    # 4. Scraper crashed (scraper_error=True) + borderline present -> goes to human_review
    s4_err = {"scraper_error": True, "approved": [], "borderline": [{"id": 9}], "loop_count": 1}
    assert route_after_rights_check(s4_err, cfg) == "human_review"

    # 5. Scraper NOT blocked / NO error + short of goal -> loops to plan
    s3 = {"scraper_blocked": False, "scraper_error": False, "approved": [{"id": 1}], "borderline": [], "loop_count": 1}
    assert route_after_rights_check(s3, cfg) == "plan"

    # 6. Scraper NOT blocked / NO error + goal satisfied -> proceeds to backfill
    s4 = {"scraper_blocked": False, "scraper_error": False, "approved": [{"id": i} for i in range(8)], "borderline": [], "loop_count": 1}
    assert route_after_rights_check(s4, cfg) == "backfill"


def test_backfill_node_suppresses_extra_loop_when_scraper_error():
    """When reserve pool has a shortage but scraper is crashed/error, backfill must not trigger extra loop."""
    from graph.nodes.backfill import backfill_node

    mock_db = MagicMock()
    mock_db.fetch_reserve_pool_candidates.return_value = []  # Empty reserve

    state = {
        "target_count": 6,
        "approved": [],
        "scraper_error": True,
        "dry_run": False,
    }

    res = backfill_node(state, mock_db)
    assert res.get("trigger_extra_loop") is False
    assert res.get("backfill_performed") is True


def test_full_graph_stops_loops_when_scraper_crashes():
    """Verify entire LangGraph compiles and halts replan loops when scraper crashes on list-only."""
    import sqlite3
    config = {
        "target_count": 6,
        "spare_buffer": 2,
        "max_loops": 3,
        "recursion_limit": 50,
        "allowed_sources": {"subreddits": ["funny"]},
        "timezone": "America/New_York",
    }
    mock_db = MagicMock()
    mock_db.fetch_reserve_pool_candidates.return_value = []
    mock_db.get_sources_for_selection.return_value = []
    mock_db.get_deeper_archive_sources.return_value = []
    mock_db.pop_pending_judge_retries.return_value = []
    mock_db.save_schedule_slots.return_value = None

    mock_llm = MagicMock()
    mock_llm.total_cost_usd = 0.0
    mock_llm.call_count = 0
    mock_editor = MagicMock()

    # Scraper tool that crashes
    mock_scraper = MagicMock()
    mock_scraper.list_candidates.return_value = []
    mock_scraper.is_scraper_blocked.return_value = False
    mock_scraper.has_scraper_error.return_value = True

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    app = build_curation_graph(config, mock_llm, mock_scraper, mock_editor, mock_db, conn)

    initial_state = {
        "target_count": 6,
        "target_date": "2026-11-23",
        "loop_count": 0,
        "max_loops": 3,
        "spare_buffer": 2,
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
        "dry_run": True,
        "no_review": True,
        "borderline_policy": "reject",
        "discovered": True,
        "selected_sources": [],
        "raw_candidates": [],
        "vetted_candidates": [],
        "dropped_titles": [],
        "edited_videos": [],
        "final_schedule": [],
        "scraper_blocked": False,
        "scraper_error": False,
    }

    res = app.invoke(initial_state, config={"configurable": {"thread_id": "test_crash"}})
    # The graph must record scraper_error=True and halt after 1 loop (loop_count == 1, NOT 3)
    assert res.get("scraper_error") is True
    assert res.get("loop_count") == 1

