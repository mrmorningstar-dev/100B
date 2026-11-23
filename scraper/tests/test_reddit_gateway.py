import os
import time
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import httpx
import pytest

from core.db import VideoDatabase
from core.reddit_gateway import RedditGateway, get_reddit_user_agent, BACKOFF_STEPS
from sources.reddit import RedditSource, select_subreddits_for_run
from sources.base import VideoCandidate


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_videos.db"
    return VideoDatabase(str(db_file))


@pytest.fixture
def temp_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class TestRedditGatewayBudget:
    def test_run_budget_limit_refuses_31st_request(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "max_requests_per_run": 30,
                "max_requests_per_day": 80,
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache)
        gw.requests_this_run = 30
        can_proceed, reason = gw.can_request("json")
        assert not can_proceed
        assert "budget reached for this run" in reason

    def test_daily_budget_limit_refuses_81st_request(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "max_requests_per_run": 30,
                "max_requests_per_day": 80,
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache)
        gw.requests_this_run = 5
        # Simulate 80 requests already logged today
        from core.reddit_gateway import get_ist_today
        today_ist = get_ist_today()
        for i in range(80):
            temp_db.log_request(today_ist, host="reddit", kind="json", status_code=200)

        can_proceed, reason = gw.can_request("json")
        assert not can_proceed
        assert "daily budget reached" in reason

    def test_cache_hits_do_not_consume_budget(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "max_requests_per_run": 30,
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"etag": "abc123etag"}
        mock_resp.text = '{"data": {"children": []}}'
        mock_http.get.return_value = mock_resp

        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
        url = "https://www.reddit.com/r/funny/hot.json"

        # First request: consumes 1 request and writes cache
        resp1, status1 = gw.request(url, layer="json")
        assert status1 == "ok"
        assert gw.requests_this_run == 1
        assert mock_http.get.call_count == 1

        # Second request: served from cache; 0 network calls, budget remains 1
        resp2, status2 = gw.request(url, layer="json")
        assert status2 == "cache_hit"
        assert gw.requests_this_run == 1
        assert mock_http.get.call_count == 1
        assert resp2.status_code == 200


class TestRedditGatewayBreaker:
    def test_two_consecutive_403s_trip_circuit_breaker(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {}
        mock_resp.text = "Forbidden"
        mock_http.get.return_value = mock_resp

        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
        url = "https://www.reddit.com/r/funny/hot.json"

        # Request 1: 403 signal recorded, consecutive = 1
        gw.request(url, layer="json")
        assert gw.consecutive_stop_signals["json"] == 1
        is_blocked, _ = gw.check_circuit_breaker("json")
        assert not is_blocked

        # Request 2: second 403 trips circuit breaker
        gw.request(url, layer="json")
        assert gw.consecutive_stop_signals["json"] == 2
        is_blocked, reason = gw.check_circuit_breaker("json")
        assert is_blocked
        assert "blocked_until" in reason or "IST" in reason

    def test_tripped_breaker_blocks_new_requests_without_outbound_calls(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        # Pre-trip breaker in database
        fut = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        temp_db.update_breaker("json", fut, 1, "error_403")

        mock_http = MagicMock()
        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)

        # Outbound call is prevented immediately
        resp, status = gw.request("https://www.reddit.com/r/funny/hot.json", layer="json")
        assert resp is None
        assert mock_http.get.call_count == 0
        assert "blocked_until" in status or "IST" in status

    def test_force_flag_bypasses_circuit_breaker(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        fut = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        temp_db.update_breaker("json", fut, 1, "error_403")

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.text = '{"data": {"children": []}}'
        mock_http.get.return_value = mock_resp

        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
        resp, status = gw.request("https://www.reddit.com/r/funny/hot.json", layer="json", force=True)
        assert resp is not None
        assert status == "ok"
        assert mock_http.get.call_count == 1

    def test_rss_404_followed_by_429_immediately_trips_and_saves_rss_breaker(self, temp_db, temp_cache):
        """When RSS encounters 404 then 429, it must immediately trip and persist RSS breaker to DB without delay."""
        config = {
            "reddit": {
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        mock_http = MagicMock()
        mock_404 = MagicMock(status_code=404, headers={}, text="Not Found")
        mock_429 = MagicMock(status_code=429, headers={"Retry-After": "10"}, text="Too Many Requests")
        mock_http.get.side_effect = [mock_404, mock_429]

        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
        url1 = "https://www.reddit.com/r/funny/hot.rss"
        url2 = "https://www.reddit.com/r/funny/top.rss"

        # Request 1: 404 recorded as stop signal 1
        gw.request(url1, layer="rss")
        assert gw.consecutive_stop_signals["rss"] == 1
        assert temp_db.get_breaker("rss") is None

        # Request 2: 429 is stop signal 2; trips breaker immediately without waiting for retry!
        gw.request(url2, layer="rss")
        assert gw.consecutive_stop_signals["rss"] == 2

        # Verify breaker is saved to DB for rss layer
        breaker_row = temp_db.get_breaker("rss")
        assert breaker_row is not None
        assert breaker_row["layer"] == "rss"
        assert breaker_row["last_status"] == "error_429"
        assert breaker_row["consecutive_failures"] == 1
        assert breaker_row["blocked_until"] is not None

        # Verify check_circuit_breaker for rss now blocks
        is_blocked, reason = gw.check_circuit_breaker("rss")
        assert is_blocked

    def test_successful_request_resets_breaker(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "rate_limit_delay": [0.0, 0.0],
            }
        }
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.text = '{"data": {"children": []}}'
        mock_http.get.return_value = mock_resp

        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
        gw.consecutive_stop_signals["json"] = 1

        gw.request("https://www.reddit.com/r/funny/hot.json", layer="json")
        assert gw.consecutive_stop_signals["json"] == 0

    def test_backoff_steps_progression(self, temp_db, temp_cache):
        config = {"reddit": {"rate_limit_delay": [0.0, 0.0]}}
        gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache)
        assert BACKOFF_STEPS == [1800, 3600, 10800, 21600]

        # First trip: 30m
        gw._trip_breaker_for_layer("json", 403)
        b1 = temp_db.get_breaker("json")
        assert b1["consecutive_failures"] == 1

        # Second trip: 1h
        gw._trip_breaker_for_layer("json", 403)
        b2 = temp_db.get_breaker("json")
        assert b2["consecutive_failures"] == 2


class TestRedditGateway429RetryAfter:
    def test_429_with_retry_after_honored_once_then_stops(self, temp_db, temp_cache):
        config = {"reddit": {"rate_limit_delay": [0.0, 0.0]}}
        mock_http = MagicMock()

        # First returns 429 with Retry-After 1s (mocked sleep)
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "1"}
        resp_429.text = "Too Many Requests"

        mock_http.get.side_effect = [resp_429, resp_429]

        with patch("time.sleep") as mock_sleep:
            gw = RedditGateway(db=temp_db, config=config, cache_dir=temp_cache, http_client=mock_http)
            resp, status = gw.request("https://www.reddit.com/r/funny/hot.json", layer="json")
            assert mock_sleep.called
            assert mock_http.get.call_count == 2
            assert "429" in status


class TestSubredditRotation:
    def test_rotation_covers_12_subreddits_across_3_runs(self, temp_db):
        all_12 = [
            "funny", "Unexpected", "instant_regret", "holdmybeer",
            "Whatcouldgowrong", "oddlysatisfying", "MadeMeSmile",
            "AnimalsBeingDerps", "ContagiousLaughter", "nonononoyes",
            "maybemaybemaybe", "funnyvideos"
        ]
        history = {}

        # Run 1: picks 4 least recently scraped
        run1 = select_subreddits_for_run(all_12, 4, history)
        assert len(run1) == 4
        assert len(set(run1)) == 4
        # Mark run1 scraped at T1
        for s in run1:
            history[s] = {"last_scraped_at": "2026-11-23T01:00:00", "yield_rate": 0.5}

        # Run 2: picks 4 different unscraped
        run2 = select_subreddits_for_run(all_12, 4, history)
        assert len(run2) == 4
        assert len(set(run1).intersection(set(run2))) == 0
        for s in run2:
            history[s] = {"last_scraped_at": "2026-11-23T02:00:00", "yield_rate": 0.5}

        # Run 3: picks remaining 4 unscraped
        run3 = select_subreddits_for_run(all_12, 4, history)
        assert len(run3) == 4
        assert len(set(run1).intersection(set(run3))) == 0
        assert len(set(run2).intersection(set(run3))) == 0

        # All 12 subreddits were covered
        total_covered = set(run1) | set(run2) | set(run3)
        assert total_covered == set(all_12)


class TestEscalation:
    def test_top_week_not_requested_when_hot_and_top_day_meet_target(self, temp_db, temp_cache):
        config = {
            "reddit": {
                "subreddits": ["funny"],
                "listings": ["hot", "top_day", "top_week"],
                "rate_limit_delay": [0.0, 0.0],
            },
            "db_path": str(temp_db.db_path),
        }
        source = RedditSource(config)
        source.praw_client = None

        cand1 = VideoCandidate(
            source="reddit",
            post_id="p1",
            post_url="url1",
            title="Vid 1",
            uploader="u1",
            score=1000,
            upvote_ratio=0.9,
            num_comments=50,
            created_utc=1000.0,
            video_url="https://v.redd.it/1",
            is_nsfw=False,
        )

        with patch.object(source, "_fetch_json_page") as mock_fetch:
            mock_fetch.return_value = ([cand1], "next_cursor_1")
            candidates = source._fetch_via_json(target_count=1, seen_ids=set())

            assert len(candidates) >= 1
            # Verify only hot was called; top_week was not reached
            call_listings = [call.args[1] for call in mock_fetch.call_args_list]
            assert "top_week" not in call_listings
