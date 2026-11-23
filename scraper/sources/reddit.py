import os
import re
import sys
import time
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from loguru import logger
import praw
from dotenv import load_dotenv

from sources.base import BaseSource, VideoCandidate
from core.http import HttpClient, RateLimiter
from core.db import VideoDatabase
from core.reddit_gateway import RedditGateway, get_reddit_user_agent, get_ist_today

# Load .env from current working directory, scraper folder, or project root
for env_path in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env", Path(__file__).resolve().parents[2] / ".env"]:
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)
load_dotenv()


def select_subreddits_for_run(
    all_subs: List[str],
    subreddits_per_run: int,
    history: Dict[str, Dict[str, Any]],
) -> List[str]:
    """
    Select subreddits_per_run subreddits from all_subs:
    - Prioritizes least-recently scraped so all subreddits get a turn (e.g. 12 subs in 3 runs).
    - Among subreddits in the same scrape tier, prioritizes highest recent yield rate.
    """
    if len(all_subs) <= subreddits_per_run:
        return list(all_subs)

    # Sort key: (last_scraped_at timestamp string or empty, -yield_rate)
    # Empty string sorts first (unscraped), then oldest timestamps, then highest yield
    def sort_key(name: str):
        h = history.get(name, {})
        last_scraped = h.get("last_scraped_at") or ""
        yield_rate = float(h.get("yield_rate") or 0.0)
        return (last_scraped, -yield_rate)

    sorted_subs = sorted(all_subs, key=sort_key)
    return sorted_subs[:subreddits_per_run]


class RedditSource(BaseSource):
    """
    Polite, gentle Reddit video scraper with three resilient layers:
      Layer 1: Official Reddit API via PRAW (if .env credentials present)
      Layer 2: Public JSON listing endpoints (.json) - extracts real engagement
      Layer 3: Reddit RSS / Atom feeds (.rss) - resilient open fallback
    Governed by RedditGateway for circuit breaking, request budgeting, spacing, and caching.
    """

    name: str = "reddit"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.reddit_cfg = config.get("reddit", {})
        self.subreddits = self.reddit_cfg.get("subreddits", [
            "funny", "Unexpected", "instant_regret", "holdmybeer",
            "Whatcouldgowrong", "oddlysatisfying", "MadeMeSmile",
            "AnimalsBeingDerps", "ContagiousLaughter", "nonononoyes",
            "maybemaybemaybe", "funnyvideos"
        ])
        self.subreddits_per_run = int(self.reddit_cfg.get("subreddits_per_run", 4))
        self.listings = self.reddit_cfg.get("listings", ["hot", "top_day", "top_week"])
        self.limit = int(self.reddit_cfg.get("limit_per_request", 100))
        self.max_pages_per_source = int(self.reddit_cfg.get("max_pages_per_source_per_run", 1))
        raw_custom = self.reddit_cfg.get("custom_sources") or config.get("custom_sources") or []
        self.custom_sources = []
        for s in raw_custom:
            if isinstance(s, dict):
                s_copy = dict(s)
                mode = s_copy.get("listing", s_copy.get("listing_mode"))
                if mode and mode not in self.listings:
                    logger.warning(
                        f"Custom source r/{s_copy.get('name')} requested unconfigured listing '{mode}'. "
                        f"Clamping to gentle listing '{self.listings[0]}'."
                    )
                    s_copy["listing"] = self.listings[0]
                    s_copy["listing_mode"] = self.listings[0]
                self.custom_sources.append(s_copy)
        self.force = bool(config.get("force", False))

        # Database connection for videos and gateway
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("PYTEST_RUNNING")
            or "pytest" in sys.modules
            or os.environ.get("TESTING")
        )
        db_path = config.get("db_path")
        if not db_path:
            if is_testing:
                import tempfile
                db_path = Path(tempfile.gettempdir()) / f"pytest_reddit_videos_{os.getpid()}.db"
            else:
                db_path = Path(__file__).resolve().parent.parent / "data" / "videos.db"
        self.db = VideoDatabase(str(db_path))

        # Backwards compatibility rate limiter and http client
        self.http = HttpClient()

        # Initialize RedditGateway with shared http client
        self.gateway = RedditGateway(db=self.db, config=config, http_client=self.http)
        self.rate_limiter = RateLimiter(min_delay=self.gateway.min_delay, max_delay=self.gateway.max_delay)

        # Locate registry database path for cursor persistence
        reg_path = config.get("registry", {}).get("db_path") or config.get("registry_db_path")
        if not reg_path:
            for cand_p in [
                Path.cwd() / "agent" / "data" / "agent.db",
                Path(__file__).resolve().parent.parent.parent / "agent" / "data" / "agent.db",
                Path(__file__).resolve().parents[2] / "agent" / "data" / "agent.db",
                Path(__file__).resolve().parent.parent / "data" / "agent.db",
                Path("agent/data/agent.db"),
                Path("../agent/data/agent.db"),
            ]:
                if cand_p.is_file():
                    reg_path = str(cand_p.resolve())
                    break
        self.registry_db_path = str(reg_path) if reg_path else None

        self.praw_client: Optional[praw.Reddit] = None
        self._init_praw()

        self.last_status = "ok"
        self.last_result: Dict[str, Any] = {}

    def _get_cursor(self, source_type: str, name: str, listing_mode: str, initial_cursor: Optional[str] = None) -> Optional[str]:
        """Never use a cursor for hot listings. Reads from registry if not provided."""
        if listing_mode == "hot":
            return None
        if initial_cursor:
            return initial_cursor
        if not self.registry_db_path or not Path(self.registry_db_path).is_file():
            return None
        try:
            conn = sqlite3.connect(self.registry_db_path, timeout=5.0)
            cur = conn.execute("""
                SELECT pagination_cursor FROM sources
                WHERE source_type = ? AND name = ? AND listing_mode = ?
            """, (source_type, name, listing_mode))
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return str(row[0])
        except Exception as e:
            logger.debug(f"Error reading cursor from registry for {name}/{listing_mode}: {e}")
        return None

    def _save_cursor(self, source_type: str, name: str, listing_mode: str, cursor: Optional[str]) -> None:
        """Persist pagination cursor back to registry table."""
        if listing_mode == "hot" or not cursor or not self.registry_db_path:
            return
        try:
            if Path(self.registry_db_path).is_file():
                conn = sqlite3.connect(self.registry_db_path, timeout=5.0)
                with conn:
                    conn.execute("""
                        UPDATE sources
                        SET pagination_cursor = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE source_type = ? AND name = ? AND listing_mode = ?
                    """, (cursor, source_type, name, listing_mode))
                conn.close()
        except Exception as e:
            logger.debug(f"Error saving cursor to registry: {e}")

    def _mark_exhausted(self, source_type: str, name: str, listing_mode: str) -> None:
        """Mark source exhausted in registry after 3 empty pages in a row."""
        if not self.registry_db_path or not Path(self.registry_db_path).is_file():
            return
        try:
            conn = sqlite3.connect(self.registry_db_path, timeout=5.0)
            with conn:
                conn.execute("""
                    UPDATE sources
                    SET status = 'exhausted', updated_at = CURRENT_TIMESTAMP
                    WHERE source_type = ? AND name = ? AND listing_mode = ?
                """, (source_type, name, listing_mode))
            conn.close()
        except Exception as e:
            logger.debug(f"Error marking source exhausted: {e}")

    def _init_praw(self) -> None:
        client_id = (os.getenv("REDDIT_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("REDDIT_CLIENT_SECRET") or "").strip()
        user_agent = get_reddit_user_agent()

        is_placeholder = any(
            ph in client_id.lower() or ph in client_secret.lower()
            for ph in ["your_reddit", "client_id_here", "client_secret_here"]
        )

        if client_id and client_secret and not is_placeholder:
            try:
                self.praw_client = praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_agent=user_agent,
                    username=os.getenv("REDDIT_USERNAME") or None,
                    password=os.getenv("REDDIT_PASSWORD") or None,
                )
            except Exception as e:
                logger.warning(f"Could not initialize PRAW: {e}. Falling back to open feeds.")
                self.praw_client = None
        else:
            self.praw_client = None

    def _get_chosen_subreddits(self) -> List[str]:
        """Rotate subreddits using history and yield data to choose subreddits_per_run subs."""
        history = self.db.get_subreddit_history()

        # If agent.db sources table exists, augment yield data
        if self.registry_db_path and Path(self.registry_db_path).is_file():
            try:
                conn = sqlite3.connect(self.registry_db_path, timeout=5.0)
                cur = conn.execute("SELECT name, yield_rate, approved_count FROM sources WHERE source_type = 'subreddit';")
                for r in cur.fetchall():
                    sub_name = r[0]
                    if sub_name not in history:
                        history[sub_name] = {}
                    if r[1] is not None and float(r[1]) > history[sub_name].get("yield_rate", 0.0):
                        history[sub_name]["yield_rate"] = float(r[1])
                    if r[2] is not None and int(r[2]) > history[sub_name].get("approved_count", 0):
                        history[sub_name]["approved_count"] = int(r[2])
                conn.close()
            except Exception as e:
                logger.debug(f"Could not augment yield from registry: {e}")

        chosen = select_subreddits_for_run(self.subreddits, self.subreddits_per_run, history)
        now_iso = datetime.now().isoformat()
        for sub in chosen:
            self.db.update_subreddit_scraped(sub, now_iso)

        logger.info(f"Subreddit rotation selected {len(chosen)}/{len(self.subreddits)} subs for this run: {chosen}")
        return chosen

    def fetch_candidates(self, target_count: int) -> List[VideoCandidate]:
        candidates: List[VideoCandidate] = []
        seen_ids = set()

        # 1. Circuit breaker pre-check
        is_blocked, block_reason = self.gateway.check_circuit_breaker(force=self.force)
        if is_blocked:
            logger.info(f"Reddit circuit breaker active ({block_reason}). Skipping fetch.")
            summary_line = self.gateway.get_summary_line()
            logger.info(summary_line)
            self.last_status = "blocked"
            self.last_result = {
                "status": "blocked",
                "candidates": [],
                "requests_used": 0,
                "count_403": 0,
                "count_429": 0,
                "blocked_until": block_reason,
            }
            return []

        # 2. Select subreddits for this run
        if self.custom_sources:
            selected_subs = [s.get("name") for s in self.custom_sources if s.get("name")]
        else:
            selected_subs = self._get_chosen_subreddits()

        # 3. Layer 1: Official Reddit API via PRAW
        if self.praw_client:
            logger.info("Reddit mode: PRAW (authenticated)")
            try:
                praw_cands = self._fetch_via_praw(target_count, seen_ids, selected_subs)
                candidates.extend(praw_cands)
                if len(candidates) >= target_count:
                    self._finalize_result(candidates, target_count)
                    return candidates
            except Exception as e:
                logger.warning(f"PRAW fetch encountered error: {e}. Falling back to open feeds.")
        else:
            logger.info("Reddit mode: open feeds (limited, may be blocked)")

        # 4. Layer 2: Public JSON listings with escalation
        if len(candidates) < target_count:
            needed = target_count - len(candidates)
            try:
                json_cands = self._fetch_via_json(needed, seen_ids, selected_subs)
                candidates.extend(json_cands)
            except Exception as e:
                logger.warning(f"Reddit JSON fetch error: {e}. Falling back to RSS.")

        # 5. Layer 3: RSS / Atom feeds (open fallback if JSON falls short or is blocked)
        if len(candidates) < target_count:
            needed = target_count - len(candidates)
            try:
                rss_cands = self._fetch_via_rss(needed, seen_ids, selected_subs)
                candidates.extend(rss_cands)
            except Exception as e:
                logger.warning(f"Reddit RSS fetch error: {e}")

        self._finalize_result(candidates, target_count)
        return candidates

    def _finalize_result(self, candidates: List[VideoCandidate], target_count: int) -> None:
        """Compile structured telemetry and print concise summary line."""
        summary_line = self.gateway.get_summary_line()
        logger.info(summary_line)

        is_all_blocked, block_reason = self.gateway.check_circuit_breaker(force=self.force)
        if is_all_blocked or (len(candidates) == 0 and (self.gateway.count_403 > 0 or self.gateway.count_429 > 0 or "open" in summary_line)):
            self.last_status = "blocked"
        elif len(candidates) >= target_count:
            self.last_status = "success"
        else:
            self.last_status = "partial"

        self.last_result = {
            "status": self.last_status,
            "candidates": candidates,
            "requests_used": self.gateway.requests_this_run,
            "count_403": self.gateway.count_403,
            "count_429": self.gateway.count_429,
            "blocked_until": block_reason,
            "summary": summary_line,
        }

    def _fetch_via_praw(self, target_count: int, seen_ids: set, selected_subs: List[str]) -> List[VideoCandidate]:
        candidates: List[VideoCandidate] = []
        is_blocked, _ = self.gateway.check_circuit_breaker(layer="praw", force=self.force)
        if is_blocked:
            return candidates

        for sub_name in selected_subs:
            if len(candidates) >= target_count:
                break

            for listing in ["hot", "top_day"]:
                if listing not in self.listings:
                    continue
                can_req, _ = self.gateway.can_request("praw")
                if not can_req:
                    return candidates

                self.gateway.record_external_request("praw")
                try:
                    sub = self.praw_client.subreddit(sub_name)
                    generator = sub.hot(limit=self.limit) if listing == "hot" else sub.top(time_filter="day", limit=self.limit)
                    for post in generator:
                        if len(candidates) >= target_count:
                            break
                        if post.id in seen_ids:
                            continue
                        seen_ids.add(post.id)
                        is_video = getattr(post, "is_video", False)
                        media = getattr(post, "media", None) or {}
                        if not is_video and not ("v.redd.it" in getattr(post, "domain", "")):
                            continue

                        cand = VideoCandidate(
                            source="reddit",
                            post_id=f"reddit_{post.id}",
                            post_url=f"https://www.reddit.com{getattr(post, 'permalink', '')}",
                            title=getattr(post, "title", ""),
                            uploader=str(getattr(post, "author", "unknown")),
                            score=int(getattr(post, "score", 0)),
                            upvote_ratio=float(getattr(post, "upvote_ratio", 0.95)),
                            num_comments=int(getattr(post, "num_comments", 0)),
                            created_utc=float(getattr(post, "created_utc", 0)),
                            video_url=getattr(post, "url", ""),
                            is_nsfw=bool(getattr(post, "over_18", False)),
                            duration=None,
                            extra={"source_feed": "praw", "subreddit": sub_name, "listing": listing},
                        )
                        candidates.append(cand)
                except Exception as e:
                    logger.warning(f"Error fetching r/{sub_name} via PRAW: {e}")
                    break

        return candidates

    def _fetch_via_json(
        self,
        target_count: int,
        seen_ids: set,
        selected_subs: Optional[List[str]] = None,
    ) -> List[VideoCandidate]:
        """
        Layer 2: Extract video candidates from Reddit JSON endpoints.
        Escalation order:
          Phase A: hot + top_day on chosen subs (page 1)
          Phase B: top_week on chosen subs (page 1) only if still short
          Phase C: top_week page 2 only if still short and max_pages > 1
        """
        candidates: List[VideoCandidate] = []
        is_blocked, _ = self.gateway.check_circuit_breaker(layer="json", force=self.force)
        if is_blocked:
            return candidates

        if selected_subs is None:
            if self.custom_sources:
                selected_subs = [s.get("name") for s in self.custom_sources if s.get("name")]
            else:
                selected_subs = self._get_chosen_subreddits()

        custom_map = {s.get("name"): s for s in self.custom_sources if isinstance(s, dict) and s.get("name")}

        # Phase A: Initial fetch on hot and top_day
        initial_listings = [l for l in ["hot", "top_day"] if l in self.listings]
        for sub_name in selected_subs:
            if len(candidates) >= target_count:
                break
            for listing in initial_listings:
                if len(candidates) >= target_count:
                    break
                initial_cursor = None
                if sub_name in custom_map and custom_map[sub_name].get("listing") == listing:
                    initial_cursor = custom_map[sub_name].get("cursor")
                new_cands, _ = self._fetch_json_page(
                    sub_name, listing, page=0, target_count=target_count, seen_ids=seen_ids, cursor=initial_cursor
                )
                candidates.extend(new_cands)
                is_layer_blocked, _ = self.gateway.check_circuit_breaker(layer="json", force=self.force)
                if is_layer_blocked:
                    return candidates

        # Phase B: Escalation to top_week (page 1) only if still short
        if len(candidates) < target_count and "top_week" in self.listings:
            logger.info(f"Candidates short ({len(candidates)}/{target_count}); escalating to top_week...")
            for sub_name in selected_subs:
                if len(candidates) >= target_count:
                    break
                initial_cursor = None
                if sub_name in custom_map and custom_map[sub_name].get("listing") == "top_week":
                    initial_cursor = custom_map[sub_name].get("cursor")
                cursor = self._get_cursor("subreddit", sub_name, "top_week", initial_cursor=initial_cursor)

                for page in range(self.max_pages_per_source):
                    new_cands, next_cursor = self._fetch_json_page(
                        sub_name, "top_week", page=page, target_count=target_count, seen_ids=seen_ids, cursor=cursor
                    )
                    candidates.extend(new_cands)
                    if next_cursor:
                        self._save_cursor("subreddit", sub_name, "top_week", next_cursor)
                        cursor = next_cursor
                    if len(candidates) >= target_count or not next_cursor or not new_cands:
                        break
                    is_layer_blocked, _ = self.gateway.check_circuit_breaker(layer="json", force=self.force)
                    if is_layer_blocked:
                        return candidates

        # Phase D: Extra listings (only if explicitly enabled in self.listings, e.g. top_month)
        extra_listings = [l for l in self.listings if l not in ["hot", "top_day", "top_week"]]
        if extra_listings and len(candidates) < target_count:
            for sub_name in selected_subs:
                if len(candidates) >= target_count:
                    break
                for listing in extra_listings:
                    if len(candidates) >= target_count:
                        break
                    initial_cursor = None
                    if sub_name in custom_map and custom_map[sub_name].get("listing") == listing:
                        initial_cursor = custom_map[sub_name].get("cursor")
                    uses_cursor = listing != "hot"
                    cursor = self._get_cursor("subreddit", sub_name, listing, initial_cursor=initial_cursor) if uses_cursor else None
                    consecutive_empty = 0

                    for page in range(self.max_pages_per_source):
                        new_cands, next_cursor = self._fetch_json_page(
                            sub_name, listing, page=page, target_count=target_count, seen_ids=seen_ids, cursor=cursor
                        )
                        candidates.extend(new_cands)
                        if uses_cursor and next_cursor:
                            self._save_cursor("subreddit", sub_name, listing, next_cursor)
                            cursor = next_cursor

                        if len(new_cands) == 0:
                            consecutive_empty += 1
                            if consecutive_empty >= 3 and uses_cursor:
                                self._mark_exhausted("subreddit", sub_name, listing)
                                break
                            if not next_cursor:
                                break
                        else:
                            consecutive_empty = 0

                        if len(candidates) >= target_count or not uses_cursor or not next_cursor:
                            break

        return candidates

    def _fetch_json_page(
        self,
        sub_name: str,
        listing: str,
        page: int,
        target_count: int,
        seen_ids: set,
        cursor: Optional[str] = None,
    ) -> Tuple[List[VideoCandidate], Optional[str]]:
        candidates: List[VideoCandidate] = []
        time_filter = listing.replace("top_", "") if listing.startswith("top_") else "all"
        uses_cursor = listing != "hot"
        if not cursor and uses_cursor:
            cursor = self._get_cursor("subreddit", sub_name, listing)

        params: Dict[str, Any] = {"limit": self.limit}
        if uses_cursor and cursor:
            params["after"] = cursor

        endpoint = "hot.json"
        if listing.startswith("top_") or listing == "top":
            endpoint = "top.json"
            params["t"] = time_filter
        elif listing == "rising":
            endpoint = "rising.json"

        url = f"https://www.reddit.com/r/{sub_name}/{endpoint}"
        logger.info(f"[JSON:{sub_name}:{listing}] Fetching page {page + 1} (cursor={cursor or 'start'})...")

        resp, status_flag = self.gateway.request(
            url,
            layer="json",
            params=params,
            force=self.force,
        )

        if not resp or resp.status_code != 200:
            return candidates, None

        after_val = None
        try:
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            after_val = data.get("data", {}).get("after")

            last_fullname = None
            for child in children:
                post = child.get("data", {})
                post_id = post.get("id")
                fullname = post.get("name") or f"t3_{post_id}"
                last_fullname = fullname

                if not post_id or post_id in seen_ids:
                    continue

                is_video = post.get("is_video", False)
                domain = post.get("domain", "")
                media = post.get("media") or {}
                reddit_vid = media.get("reddit_video") if isinstance(media, dict) else {}

                crosspost_list = post.get("crosspost_parent_list") or []
                if not reddit_vid and crosspost_list and isinstance(crosspost_list, list) and len(crosspost_list) > 0:
                    cp = crosspost_list[0]
                    if isinstance(cp, dict):
                        cp_media = cp.get("media") or {}
                        if isinstance(cp_media, dict) and "reddit_video" in cp_media:
                            reddit_vid = cp_media.get("reddit_video") or {}
                        if cp.get("is_video"):
                            is_video = True

                if not (is_video or "v.redd.it" in domain or reddit_vid):
                    continue

                seen_ids.add(post_id)
                permalink = post.get("permalink", "")
                dur = None
                fallback_url = None
                if reddit_vid and isinstance(reddit_vid, dict):
                    raw_dur = reddit_vid.get("duration")
                    if raw_dur is not None and float(raw_dur) > 0:
                        dur = float(raw_dur)
                    fallback_url = reddit_vid.get("fallback_url")

                post_url = f"https://www.reddit.com{permalink}"
                video_url = post_url
                url_field = post.get("url", "")
                if url_field.startswith("https://v.redd.it/"):
                    video_url = url_field

                cand = VideoCandidate(
                    source="reddit",
                    post_id=f"reddit_{post_id}",
                    post_url=post_url,
                    title=post.get("title", ""),
                    uploader=post.get("author", "unknown"),
                    score=int(post.get("score", 0)),
                    upvote_ratio=float(post.get("upvote_ratio", 0.95)),
                    num_comments=int(post.get("num_comments", 0)),
                    created_utc=float(post.get("created_utc", 0)),
                    video_url=video_url,
                    is_nsfw=bool(post.get("over_18", False)),
                    duration=dur,
                    extra={
                        "source_type": "subreddit",
                        "source_name": sub_name,
                        "listing_mode": listing,
                        "subreddit": post.get("subreddit", sub_name),
                        "listing": listing,
                        "source_feed": "json",
                        "fallback_url": fallback_url,
                        "pagination_cursor": fullname if listing != "hot" else None,
                    },
                )
                candidates.append(cand)

            next_cursor = after_val or last_fullname
            if uses_cursor and next_cursor:
                self._save_cursor("subreddit", sub_name, listing, next_cursor)

            logger.info(f"[JSON:{sub_name}:{listing}] Page {page + 1}: found {len(candidates)} candidates.")
        except Exception as e:
            logger.warning(f"Error parsing Reddit JSON for r/{sub_name}: {e}")

        return candidates, after_val

    def _fetch_via_rss(
        self,
        target_count: int,
        seen_ids: set,
        selected_subs: Optional[List[str]] = None,
    ) -> List[VideoCandidate]:
        """
        Layer 3: Extract video candidates from Reddit Atom/RSS feeds.
        Honors same rotation, circuit breaker, spacing, and escalation.
        """
        candidates: List[VideoCandidate] = []
        is_blocked, _ = self.gateway.check_circuit_breaker(layer="rss", force=self.force)
        if is_blocked:
            return candidates

        if selected_subs is None:
            if self.custom_sources:
                selected_subs = [s.get("name") for s in self.custom_sources if s.get("name")]
            else:
                selected_subs = self._get_chosen_subreddits()

        listing_baselines = {
            "top_day": {"base_upvotes": 15000, "decay": 0.12},
            "top_week": {"base_upvotes": 20000, "decay": 0.12},
            "hot": {"base_upvotes": 8000, "decay": 0.15},
        }

        # Query multireddit of the selected subreddits for polite bulk retrieval
        multi_slug = "+".join(selected_subs)
        rss_listings = [l for l in ["hot", "top_day"] if l in self.listings]

        for listing in rss_listings:
            if len(candidates) >= target_count:
                break
            time_filter = listing.replace("top_", "") if listing.startswith("top_") else "all"
            endpoint = "hot.rss?limit=100" if listing == "hot" else f"top.rss?t={time_filter}&limit=100"
            url = f"https://www.reddit.com/r/{multi_slug}/{endpoint}"

            resp, status_flag = self.gateway.request(url, layer="rss", force=self.force)
            if not resp or resp.status_code != 200:
                is_layer_blocked, _ = self.gateway.check_circuit_breaker(layer="rss", force=self.force)
                if is_layer_blocked:
                    return candidates
                continue

            try:
                root = ET.fromstring(resp.text)
                entries = root.findall("{http://www.w3.org/2005/Atom}entry")
                for entry_idx, entry in enumerate(entries):
                    if len(candidates) >= target_count:
                        break
                    link_elem = entry.find("{http://www.w3.org/2005/Atom}link")
                    post_url = link_elem.attrib.get("href", "") if link_elem is not None else ""
                    id_match = re.search(r"/comments/([a-zA-Z0-9]+)/", post_url)
                    if not id_match:
                        continue
                    post_id = id_match.group(1)
                    if post_id in seen_ids:
                        continue

                    content_elem = entry.find("{http://www.w3.org/2005/Atom}content")
                    content_text = content_elem.text if content_elem is not None else ""
                    v_match = re.search(r"https://v\.redd\.it/[a-zA-Z0-9]+", content_text)
                    if not v_match:
                        continue

                    seen_ids.add(post_id)
                    title_elem = entry.find("{http://www.w3.org/2005/Atom}title")
                    title = title_elem.text if title_elem is not None else f"Video #{post_id}"
                    author_elem = entry.find("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name")
                    author = author_elem.text.replace("/u/", "") if author_elem is not None else "unknown"

                    baseline = listing_baselines.get(listing, listing_baselines["hot"])
                    decay = 1.0 + (baseline["decay"] * entry_idx)
                    est_score = max(50, int(baseline["base_upvotes"] / decay))

                    candidates.append(VideoCandidate(
                        source="reddit",
                        post_id=f"reddit_{post_id}",
                        post_url=post_url,
                        title=title,
                        uploader=author,
                        score=est_score,
                        upvote_ratio=0.92,
                        num_comments=max(5, int(est_score * 0.04)),
                        created_utc=time.time(),
                        video_url=v_match.group(0),
                        is_nsfw=False,
                        duration=None,
                        extra={"source_feed": "multireddit_rss", "listing": listing, "is_estimated_score": True},
                    ))
            except Exception as e:
                logger.warning(f"Error parsing RSS XML: {e}")

        return candidates
