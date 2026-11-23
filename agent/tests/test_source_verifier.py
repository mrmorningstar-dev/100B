import json
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

from core.db import AgentDatabase
from core.source_verifier import verify_sources, verify_subreddit_online


class TestSourceVerifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_agent.db"
        self.db = AgentDatabase(self.db_path)
        self.db.seed_default_sources(["funny", "Unexpected"])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reject_already_existing_source(self):
        candidates = [
            {"source_type": "subreddit", "name": "funny", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "r/Unexpected", "listing_mode": "hot"},
        ]
        verified, rejected = verify_sources(candidates, self.db, skip_online_check=True)
        self.assertEqual(len(verified), 0)
        self.assertEqual(len(rejected), 2)
        reasons = [r["reason"] for r in rejected]
        self.assertEqual(reasons, ["already_exists_in_registry", "already_exists_in_registry"])

    def test_reject_invalid_subreddit_format(self):
        candidates = [
            {"source_type": "subreddit", "name": "bad sub with spaces!", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "x", "listing_mode": "hot"}, # too short (<3)
        ]
        verified, rejected = verify_sources(candidates, self.db, skip_online_check=True)
        self.assertEqual(len(verified), 0)
        self.assertEqual(len(rejected), 2)
        self.assertEqual(rejected[0]["reason"], "invalid_subreddit_format")
        self.assertEqual(rejected[1]["reason"], "invalid_subreddit_format")

    def test_cap_trial_sources_at_5(self):
        candidates = [
            {"source_type": "subreddit", "name": f"new_comedy_sub_{i}", "listing_mode": "hot"}
            for i in range(8)
        ]
        verified, rejected = verify_sources(candidates, self.db, max_daily_trials=5, skip_online_check=True)
        self.assertEqual(len(verified), 5)
        self.assertEqual(len(rejected), 3)
        self.assertIn("daily_trial_cap_reached_5", rejected[0]["reason"])

    @patch("core.source_verifier.verify_subreddit_online")
    def test_online_verification_drops_nsfw_or_no_video(self, mock_online):
        mock_online.side_effect = lambda name, **kwargs: {
            "good_sub": (True, "ok"),
            "nsfw_sub": (False, "over18_or_nsfw"),
            "text_sub": (False, "videos_not_allowed"),
        }.get(name, (False, "unknown"))

        candidates = [
            {"source_type": "subreddit", "name": "good_sub", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "nsfw_sub", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "text_sub", "listing_mode": "hot"},
        ]
        verified, rejected = verify_sources(candidates, self.db, skip_online_check=False)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["name"], "good_sub")
        self.assertEqual(len(rejected), 2)
        self.assertEqual(rejected[0]["reason"], "over18_or_nsfw")
        self.assertEqual(rejected[1]["reason"], "videos_not_allowed")

    @patch("subprocess.run")
    def test_verify_subreddit_online_about_json_valid(self, mock_subproc):
        """about.json returns valid public comedy subreddit."""
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = json.dumps({
            "kind": "t5",
            "data": {
                "title": "Laugh Out Loud",
                "over18": False,
                "subscribers": 150000,
                "subreddit_type": "public",
                "allow_videos": True,
                "public_description": "A place for hilarious memes and clips",
            }
        })
        mock_subproc.return_value = mock_res

        is_valid, reason = verify_subreddit_online("laugh_out_loud", min_subscribers=5000)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "ok")

    @patch("subprocess.run")
    def test_verify_subreddit_online_about_json_drops_over18_and_low_subscribers(self, mock_subproc):
        """about.json rejects NSFW, non-existent 404, rate limit 429, and tiny subreddits."""
        # 1. Over 18
        mock_res = MagicMock(returncode=0, stdout=json.dumps({
            "kind": "t5", "data": {"over18": True, "subscribers": 50000}
        }))
        mock_subproc.return_value = mock_res
        is_valid, reason = verify_subreddit_online("bad_sub")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "over18_or_nsfw")

        # 2. 404 Not Found
        mock_subproc.return_value = MagicMock(returncode=0, stdout=json.dumps({
            "error": 404, "message": "Not Found"
        }))
        is_valid, reason = verify_subreddit_online("missing_sub")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "subreddit_does_not_exist")

        # 3. 429 Rate Limited
        mock_subproc.return_value = MagicMock(returncode=0, stdout=json.dumps({
            "error": 429, "message": "Too Many Requests"
        }))
        is_valid, reason = verify_subreddit_online("flooded_sub")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "rate_limited_429")

        # 4. Low subscribers (< 5,000)
        mock_subproc.return_value = MagicMock(returncode=0, stdout=json.dumps({
            "kind": "t5", "data": {"over18": False, "subscribers": 420, "subreddit_type": "public"}
        }))
        is_valid, reason = verify_subreddit_online("tiny_sub")
        self.assertFalse(is_valid)
        self.assertEqual(reason, "too_few_subscribers_420")

    @patch("time.sleep")
    @patch("core.source_verifier.verify_subreddit_online")
    def test_inter_call_waiting_is_enforced(self, mock_online, mock_sleep):
        """Verifier enforces waiting delay between consecutive online verification calls."""
        mock_online.return_value = (True, "ok")
        candidates = [
            {"source_type": "subreddit", "name": "sub_one", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "sub_two", "listing_mode": "hot"},
            {"source_type": "subreddit", "name": "sub_three", "listing_mode": "hot"},
        ]
        verified, rejected = verify_sources(
            candidates,
            self.db,
            skip_online_check=False,
            inter_call_delay_seconds=2.0,
        )
        self.assertEqual(len(verified), 3)
        # Sleep must be called between consecutive checks (2 times for 3 subreddits)
        self.assertGreaterEqual(mock_sleep.call_count, 2)

    def test_verify_subreddit_online_skips_when_circuit_breaker_open(self):
        """When circuit breaker is open, verify_subreddit_online skips without making request."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.side_effect = lambda layer: (True, "about blocked until 11:07 AM IST") if layer == "about" else (False, None)

        is_valid, reason = verify_subreddit_online("StartledCats", gateway=mock_gateway)
        self.assertFalse(is_valid)
        self.assertIn("circuit_breaker_open", reason)
        mock_gateway.request.assert_not_called()

    def test_verify_subreddit_online_skips_when_json_breaker_open(self):
        """When JSON breaker is open, about.json check skips without making request."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.side_effect = lambda layer: (True, "json blocked until 11:07 AM IST") if layer == "json" else (False, None)

        is_valid, reason = verify_subreddit_online("StartledCats", gateway=mock_gateway)
        self.assertFalse(is_valid)
        self.assertIn("circuit_breaker_open", reason)
        mock_gateway.request.assert_not_called()

    def test_verify_subreddit_online_routes_through_gateway(self):
        """When breaker is closed, verify_subreddit_online calls gateway.request with layer='about'."""
        import httpx
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (False, None)
        mock_gateway.can_request.return_value = (True, "ok")
        payload = {
            "kind": "t5",
            "data": {
                "title": "Startled Cats",
                "over18": False,
                "subscribers": 120000,
                "subreddit_type": "public",
                "allow_videos": True,
                "public_description": "Cats jumping at cucumbers",
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(payload)
        mock_gateway.request.return_value = (mock_resp, "ok")

        is_valid, reason = verify_subreddit_online("StartledCats", gateway=mock_gateway)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "ok")
        mock_gateway.request.assert_called_once_with(
            "https://www.reddit.com/r/StartledCats/about.json",
            layer="about",
            use_cache=True,
            ttl_seconds=86400,
        )


if __name__ == "__main__":
    unittest.main()
