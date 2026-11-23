import pytest
from pathlib import Path
from unittest.mock import MagicMock
from graph.nodes.plan import plan_node
from llm.schemas import PlanResult


def test_plan_node_loop_1_reddit_only():
    config = {
        "allowed_sources": {
            "subreddits": ["funny", "Unexpected", "MadeMeSmile"],
            "twitter_accounts": ["LaughsDaily"],
            "twitter_enabled": False,
        }
    }
    state = {
        "loop_count": 0,
        "target_count": 24,
        "spare_buffer": 4,
        "approved": [],
        "borderline": [],
        "sources_tried": [],
    }
    mock_llm = MagicMock()
    result = plan_node(state, config, mock_llm)

    # In loop 1, LLM is not called; Twitter is skipped
    mock_llm.call_structured.assert_not_called()
    assert result["loop_count"] == 1
    assert result["plan"]["twitter_accounts"] == []
    assert len(result["plan"]["subreddits"]) > 0


def test_plan_node_loop_2_skips_twitter_when_disabled():
    config = {
        "allowed_sources": {
            "subreddits": ["funny", "Unexpected", "AnimalsBeingDerps"],
            "twitter_accounts": ["LaughsDaily", "DailyViralVids"],
            "twitter_enabled": False,
        }
    }
    state = {
        "loop_count": 1,
        "target_count": 24,
        "spare_buffer": 4,
        "approved": [],
        "borderline": [],
        "rejected": [{"reason": "low_resolution"}],
        "sources_tried": ["funny"],
    }
    mock_llm = MagicMock()
    # Even if LLM returns a twitter account in schema
    mock_llm.call_structured.return_value = PlanResult(
        subreddits=["Unexpected", "AnimalsBeingDerps"],
        twitter_accounts=["LaughsDaily"],
        strategy_reason="Pivot to animals and unexpected",
        proposed_new_sources=[],
    )

    result = plan_node(state, config, mock_llm)

    assert result["loop_count"] == 2
    # Because twitter_enabled is False, twitter_accounts must be filtered out
    assert result["plan"]["twitter_accounts"] == []
    assert result["plan"]["subreddits"] == ["Unexpected", "AnimalsBeingDerps"]


def test_plan_node_loop_2_fallback_skips_twitter_when_disabled():
    config = {
        "allowed_sources": {
            "subreddits": ["funny", "Unexpected"],
            "twitter_accounts": ["LaughsDaily"],
            "twitter_enabled": False,
        }
    }
    state = {
        "loop_count": 1,
        "target_count": 24,
        "spare_buffer": 4,
        "approved": [],
        "borderline": [],
        "rejected": [],
        "sources_tried": ["funny"],
    }
    mock_llm = MagicMock()
    mock_llm.call_structured.side_effect = RuntimeError("LLM failure")

    result = plan_node(state, config, mock_llm)

    assert result["loop_count"] == 2
    assert result["plan"]["twitter_accounts"] == []
    assert "Unexpected" in result["plan"]["subreddits"]


def test_plan_node_expands_goal_when_reserve_deficit_exists():
    config = {
        "allowed_sources": {
            "subreddits": ["funny", "Unexpected"],
            "twitter_enabled": False,
        },
        "reserve_pool": {
            "min_stock": 6,
            "target_stock": 12,
            "stock_buffer_per_run": 2,
        },
    }
    state = {
        "loop_count": 0,
        "target_count": 6,
        "spare_buffer": 2,
        "approved": [],
        "borderline": [],
        "sources_tried": [],
    }
    mock_llm = MagicMock()
    mock_db = MagicMock()
    # Reserve has only 1 clip (deficit of 11, capped by stock_buffer_per_run=2)
    mock_db.fetch_reserve_pool_candidates.return_value = [{"post_id": "r1"}]

    result = plan_node(state, config, mock_llm, db=mock_db)
    # Target needed = target_count (6) + buffer (min(2, 3) = 2) + reserve_deficit (2) = 10
    # target_to_fetch = int(10 * 1.5) = 15
    assert result["plan"]["target_to_fetch"] == 15


def test_plan_node_respects_allowed_listings_in_loops_2_and_3():
    """Loops 2 and 3 must query only allowed listings and never touch top_month/top_year."""
    config = {
        "reddit": {
            "listings": ["hot", "top_day", "top_week"],
            "max_requests_per_run": 30,
            "max_requests_per_day": 80,
            "subreddits_per_run": 4,
        },
        "allowed_sources": {
            "subreddits": ["funny", "Unexpected"],
            "twitter_enabled": False,
        },
    }
    state = {
        "loop_count": 1,
        "target_count": 6,
        "approved": [],
        "borderline": [],
        "sources_tried": ["funny"],
    }
    mock_llm = MagicMock()
    mock_db = MagicMock()
    mock_db.get_deeper_archive_sources.return_value = [
        {"name": "Unexpected", "listing_mode": "top_week", "source_type": "subreddit"}
    ]

    mock_gateway = MagicMock()
    mock_gateway.check_circuit_breaker.return_value = (False, "ok")
    mock_gateway.requests_this_run = 4
    mock_gateway.max_requests_per_run = 30
    mock_gateway.max_requests_per_day = 80
    mock_gateway.db.get_requests_count_today.return_value = 10

    result = plan_node(state, config, mock_llm, db=mock_db, gateway=mock_gateway)

    assert result["loop_count"] == 2
    mock_db.get_deeper_archive_sources.assert_called_once()
    _, kwargs = mock_db.get_deeper_archive_sources.call_args
    assert kwargs.get("allowed_listings") == ["hot", "top_day", "top_week"]
    for s in result["selected_sources"]:
        assert s["listing_mode"] in ["hot", "top_day", "top_week"]
        assert s["listing_mode"] not in ["top_month", "top_year", "top_all"]


def test_plan_node_bypasses_when_budget_exhausted():
    """When Reddit request budget is 0, planner skips scraping and flags budget_exhausted."""
    config = {
        "reddit": {
            "listings": ["hot", "top_day", "top_week"],
            "max_requests_per_run": 30,
            "max_requests_per_day": 80,
        },
        "allowed_sources": {"subreddits": ["funny"]},
    }
    state = {
        "loop_count": 1,
        "target_count": 6,
        "approved": [],
        "borderline": [],
        "sources_tried": ["funny"],
    }
    mock_llm = MagicMock()
    mock_gateway = MagicMock()
    mock_gateway.check_circuit_breaker.return_value = (False, "ok")
    mock_gateway.requests_this_run = 30  # Run budget exhausted
    mock_gateway.max_requests_per_run = 30
    mock_gateway.max_requests_per_day = 80
    mock_gateway.db.get_requests_count_today.return_value = 50

    result = plan_node(state, config, mock_llm, gateway=mock_gateway)

    assert result["plan"]["subreddits"] == []
    assert result["selected_sources"] == []
    assert result["budget_exhausted"] is True
    assert "budget is exhausted" in result["plan"]["strategy_reason"]


def test_db_get_deeper_archive_sources_never_returns_heavy_lists():
    """AgentDatabase.get_deeper_archive_sources strictly respects allowed_listings."""
    import tempfile
    from core.db import AgentDatabase

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_gentle.db"
        db = AgentDatabase(db_path)

        # Seed with diverse listings including old heavy ones
        with db._get_connection() as conn:
            for l in ["hot", "top_day", "top_week", "top_month", "top_year"]:
                conn.execute("""
                    INSERT INTO sources (source_type, name, listing_mode, status, added_by, created_at, updated_at)
                    VALUES ('subreddit', 'funny', ?, 'active', 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (l,))

        # Loop 2 with gentle config
        sources_l2 = db.get_deeper_archive_sources(
            loop_count=2,
            limit=4,
            allowed_listings=["hot", "top_day", "top_week"],
        )
        assert len(sources_l2) > 0
        for s in sources_l2:
            assert s["listing_mode"] in ["top_week", "top_day"]
            assert s["listing_mode"] not in ["top_month", "top_year", "top_all"]

        # Loop 3 with gentle config
        sources_l3 = db.get_deeper_archive_sources(
            loop_count=3,
            limit=4,
            allowed_listings=["hot", "top_day", "top_week"],
        )
        assert len(sources_l3) > 0
        for s in sources_l3:
            assert s["listing_mode"] in ["hot", "top_day", "top_week"]
            assert s["listing_mode"] not in ["top_month", "top_year", "top_all"]



