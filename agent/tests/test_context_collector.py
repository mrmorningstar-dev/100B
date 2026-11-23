import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from core.context_collector import fetch_recent_titles, collect_fresh_context


class TestContextCollector(unittest.TestCase):
    """Test suite verifying context collection behavior and Reddit gateway routing."""

    def test_fetch_recent_titles_skips_when_circuit_breaker_open(self):
        """When RSS circuit breaker is open, fetch_recent_titles skips without making requests."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (True, "cooldown_active")

        titles = fetch_recent_titles(["funny", "Unexpected"], gateway=mock_gateway)
        self.assertEqual(titles, [])
        mock_gateway.check_circuit_breaker.assert_called_once_with(layer="rss")
        mock_gateway.request.assert_not_called()

    def test_fetch_recent_titles_skips_when_budget_exhausted(self):
        """When RSS budget is exhausted, fetch_recent_titles skips without making requests."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (False, "ok")
        mock_gateway.can_request.return_value = (False, "daily_budget_exhausted")

        titles = fetch_recent_titles(["funny"], gateway=mock_gateway)
        self.assertEqual(titles, [])
        mock_gateway.check_circuit_breaker.assert_called_once_with(layer="rss")
        mock_gateway.can_request.assert_called_once_with("rss")
        mock_gateway.request.assert_not_called()

    def test_fetch_recent_titles_routes_through_gateway_success(self):
        """When breaker is closed and budget available, routes through gateway and parses titles."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (False, "ok")
        mock_gateway.can_request.return_value = (True, "ok")

        sample_atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>First Hilarious Cat Video</title>
            </entry>
            <entry>
                <title>Second Amazing Dog Stunt</title>
            </entry>
        </feed>"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = sample_atom_xml
        mock_gateway.request.return_value = (mock_resp, "ok")

        titles = fetch_recent_titles(["funny", "Unexpected"], limit=10, gateway=mock_gateway)
        self.assertEqual(titles, ["First Hilarious Cat Video", "Second Amazing Dog Stunt"])
        mock_gateway.request.assert_called_once_with(
            "https://www.reddit.com/r/funny+Unexpected/hot.rss",
            layer="rss",
            use_cache=True,
            ttl_seconds=3600,
        )

    def test_fetch_recent_titles_handles_404_gracefully(self):
        """When gateway returns a 404 response, returns empty list without raising."""
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (False, "ok")
        mock_gateway.can_request.return_value = (True, "ok")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_gateway.request.return_value = (mock_resp, "http_404")

        titles = fetch_recent_titles(["nonexistent_sub_12345"], gateway=mock_gateway)
        self.assertEqual(titles, [])
        mock_gateway.request.assert_called_once()

    def test_collect_fresh_context_integrates_gateway(self):
        """collect_fresh_context propagates gateway to fetch_recent_titles and returns summary."""
        mock_db = MagicMock()
        mock_db.get_performance_summary.return_value = {
            "active_sources": ["funny"],
            "trial_sources": ["Unexpected"],
            "already_tried_names": ["funny", "Unexpected"],
            "top_reject_reasons": ["nsfw"],
        }
        mock_gateway = MagicMock()
        mock_gateway.check_circuit_breaker.return_value = (True, "cooldown_active")

        result = collect_fresh_context(
            db=mock_db,
            config={"allowed_sources": {"subreddits": ["funny"]}},
            gateway=mock_gateway,
        )

        self.assertEqual(result["fresh_titles"], [])
        self.assertEqual(result["active_sources"], ["funny"])
        self.assertEqual(result["trial_sources"], ["Unexpected"])
        self.assertEqual(result["already_tried_names"], ["funny", "Unexpected"])
        self.assertEqual(result["top_reject_reasons"], ["nsfw"])


if __name__ == "__main__":
    unittest.main()
