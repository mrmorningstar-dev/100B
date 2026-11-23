import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger

from core.db import AgentDatabase


def fetch_recent_titles(
    subreddits: Optional[List[str]] = None,
    limit: int = 25,
    gateway: Optional[Any] = None,
    scraper_tool: Optional[Any] = None,
) -> List[str]:
    """
    Fetch recent top/hot titles from seed subreddits via multireddit Atom/RSS.
    Deterministic plain code, zero LLM calls.
    Routes through ScraperTool CLI (or skips if RSS breaker is open / budget reached).
    Resilient against network drops (falls back gracefully to empty list).
    """
    subs = subreddits or ["funny", "Unexpected", "holdmybeer", "Whatcouldgowrong", "instant_regret", "videos"]
    multi_slug = "+".join(subs[:6])

    # Direct gateway object (used in unit tests)
    if gateway is not None:
        is_blocked, block_reason = gateway.check_circuit_breaker(layer="rss")
        if is_blocked:
            logger.info(f"Reddit RSS circuit breaker is open ({block_reason}); skipping fresh context fetch.")
            return []
        can_req, budget_reason = gateway.can_request("rss")
        if not can_req:
            logger.warning(f"Reddit request budget reached ({budget_reason}); skipping fresh context fetch.")
            return []
        url = f"https://www.reddit.com/r/{multi_slug}/hot.rss"
        resp, status_flag = gateway.request(url, layer="rss", use_cache=True, ttl_seconds=3600)
        if not resp or resp.status_code != 200:
            logger.info(f"Reddit fresh context collection deferred (status={getattr(resp, 'status_code', None)}). Proceeding with empty context.")
            return []
        import xml.etree.ElementTree as ET
        titles: List[str] = []
        try:
            root = ET.fromstring(resp.text)
            for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
                if len(titles) >= limit:
                    break
                t_elem = entry.find("{http://www.w3.org/2005/Atom}title")
                if t_elem is not None and t_elem.text:
                    titles.append(t_elem.text.strip())
            return titles
        except Exception:
            return []

    # ScraperTool CLI subprocess path (production / isolated process)
    if scraper_tool is None:
        try:
            from tools.scraper_tool import ScraperTool
            scraper_tool = ScraperTool()
        except Exception as e:
            logger.debug(f"Could not initialize ScraperTool in fetch_recent_titles: {e}")
            scraper_tool = None

    if scraper_tool is not None:
        res = scraper_tool.recent_titles(multi_slug, limit=limit)
        if res.get("status") == "ok":
            titles = res.get("titles", [])
            logger.info(f"Fresh context collector fetched {len(titles)} titles from /r/{multi_slug}")
            return titles
        else:
            logger.info(f"Reddit fresh context collection deferred ({res.get('reason')}). Proceeding with empty context.")
            return []

    return []


def collect_fresh_context(
    db: AgentDatabase,
    config: Optional[Dict[str, Any]] = None,
    gateway: Optional[Any] = None,
    scraper_tool: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Gathers fresh context without LLM calls:
      - Fresh titles from 5 seed subreddits + trending videos
      - Registry stats (approval rate and top reject reasons per source)
      - Already-tried sources list
    """
    cfg = config or {}
    seed_subs = cfg.get("allowed_sources", {}).get("subreddits") or [
        "funny", "Unexpected", "holdmybeer", "Whatcouldgowrong", "instant_regret", "videos"
    ]
    fresh_titles = fetch_recent_titles(seed_subs, limit=25, gateway=gateway, scraper_tool=scraper_tool)
    perf_summary = db.get_performance_summary()

    return {
        "fresh_titles": fresh_titles,
        "active_sources": perf_summary.get("active_sources", []),
        "trial_sources": perf_summary.get("trial_sources", []),
        "already_tried_names": perf_summary.get("already_tried_names", []),
        "top_reject_reasons": perf_summary.get("top_reject_reasons", []),
    }
