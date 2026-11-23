import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger

# Add scraper directory to sys.path to reuse HttpClient if available
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
SCRAPER_DIR = PROJECT_ROOT / "scraper"
if str(SCRAPER_DIR) not in sys.path:
    sys.path.insert(0, str(SCRAPER_DIR))

try:
    from core.http import HttpClient
except ImportError:
    HttpClient = None

from core.db import AgentDatabase


def fetch_recent_titles(
    subreddits: Optional[List[str]] = None,
    limit: int = 25,
    gateway: Optional[Any] = None,
) -> List[str]:
    """
    Fetch recent top/hot titles from seed subreddits via multireddit Atom/RSS.
    Deterministic plain code, zero LLM calls.
    Routes through RedditGateway (or skips if RSS breaker is open).
    Resilient against network drops (falls back gracefully to empty list).
    """
    # 1. Initialize gateway if not passed
    if gateway is None:
        try:
            try:
                from scraper.core.reddit_gateway import RedditGateway
            except ImportError:
                from core.reddit_gateway import RedditGateway
            gateway = RedditGateway()
        except Exception as e:
            logger.debug(f"Could not initialize RedditGateway: {e}")
            gateway = None

    # 2. Skip if RSS circuit breaker is open
    if gateway is not None:
        is_blocked, block_reason = gateway.check_circuit_breaker(layer="rss")
        if is_blocked:
            logger.info(f"Reddit RSS circuit breaker is open ({block_reason}); skipping fresh context fetch.")
            return []
        can_req, budget_reason = gateway.can_request("rss")
        if not can_req:
            logger.warning(f"Reddit request budget reached ({budget_reason}); skipping fresh context fetch.")
            return []

    subs = subreddits or ["funny", "Unexpected", "holdmybeer", "Whatcouldgowrong", "instant_regret", "videos"]
    multi_slug = "+".join(subs[:6])
    url = f"https://www.reddit.com/r/{multi_slug}/hot.rss"

    titles: List[str] = []
    try:
        if gateway is not None:
            resp, status_flag = gateway.request(url, layer="rss", use_cache=True, ttl_seconds=3600)
            if not resp or resp.status_code != 200:
                logger.warning(f"Failed to fetch fresh context RSS from /r/{multi_slug} (status={getattr(resp, 'status_code', None)}, flag={status_flag})")
                return titles
            xml_text = resp.text
        elif HttpClient:
            client = HttpClient(timeout=10.0, max_retries=2)
            resp = client.get(url)
            if not resp or resp.status_code != 200:
                logger.warning(f"Failed to fetch fresh context RSS from /r/{multi_slug}")
                return titles
            xml_text = resp.text
        else:
            return titles

        root = ET.fromstring(xml_text)
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            if len(titles) >= limit:
                break
            t_elem = entry.find("{http://www.w3.org/2005/Atom}title")
            if t_elem is not None and t_elem.text:
                titles.append(t_elem.text.strip())

        logger.info(f"Fresh context collector fetched {len(titles)} titles from /r/{multi_slug}")
    except Exception as e:
        logger.warning(f"Error collecting fresh context titles: {e}. Proceeding with empty context.")

    return titles


def collect_fresh_context(
    db: AgentDatabase,
    config: Optional[Dict[str, Any]] = None,
    gateway: Optional[Any] = None,
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
    fresh_titles = fetch_recent_titles(seed_subs, limit=25, gateway=gateway)
    perf_summary = db.get_performance_summary()

    return {
        "fresh_titles": fresh_titles,
        "active_sources": perf_summary.get("active_sources", []),
        "trial_sources": perf_summary.get("trial_sources", []),
        "already_tried_names": perf_summary.get("already_tried_names", []),
        "top_reject_reasons": perf_summary.get("top_reject_reasons", []),
    }
