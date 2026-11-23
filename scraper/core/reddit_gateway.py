import os
import sys
import re
import time
import json
import random
import hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union
from loguru import logger
import httpx

try:
    from core.db import VideoDatabase
except ImportError:
    from scraper.core.db import VideoDatabase

IST_TZ = ZoneInfo("Asia/Kolkata")
BACKOFF_STEPS = [1800, 3600, 10800, 21600]  # 30 min, 1 hour, 3 hours, 6 hours


def get_reddit_user_agent() -> str:
    """Return honest, descriptive User-Agent required by Reddit."""
    custom_ua = os.getenv("REDDIT_USER_AGENT")
    if custom_ua and "YOUR_USERNAME" not in custom_ua and "your_username" not in custom_ua:
        return custom_ua.strip()
    username = (os.getenv("REDDIT_USERNAME") or "").strip() or "unknown"
    return f"python:funny_video_curator:v1.0 (by /u/{username})"


def get_ist_today() -> str:
    """Return current date in Asia/Kolkata (YYYY-MM-DD)."""
    return datetime.now(IST_TZ).strftime("%Y-%m-%d")


class RedditGateway:
    """
    Unified gatekeeper for ALL outbound Reddit interactions (JSON, RSS, PRAW, about.json).
    Enforces:
      - Honest User-Agent header (no browser spoofing)
      - Circuit breaker per layer with persistent exponential backoff
      - Strict daily and per-run request budgets
      - Jittered rate-limit spacing across all layers
      - Persistent on-disk caching with conditional ETag/304 handling
      - Structured execution telemetry
    """

    def __init__(
        self,
        db: Optional[VideoDatabase] = None,
        config: Optional[Dict[str, Any]] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        http_client: Optional[Any] = None,
    ):
        self.config = (config or {}).get("reddit", config or {})
        self.max_requests_per_run = int(self.config.get("max_requests_per_run", 30))
        self.max_requests_per_day = int(self.config.get("max_requests_per_day", 80))
        delay_range = self.config.get("rate_limit_delay", [6.0, 12.0])
        self.min_delay = float(delay_range[0])
        self.max_delay = float(delay_range[1])
        self.cooldown_after_block = float(self.config.get("cooldown_after_block_seconds", 120))

        if http_client is not None:
            self.http = http_client
        else:
            from core.http import HttpClient
            self.http = HttpClient()

        # Default db location
        if db is not None:
            self.db = db
        else:
            default_db_path = Path(__file__).resolve().parent.parent / "data" / "videos.db"
            self.db = VideoDatabase(str(default_db_path))

        # Default cache location
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("PYTEST_RUNNING")
            or "pytest" in sys.modules
            or os.environ.get("TESTING")
        )
        if cache_dir:
            self.cache_dir = Path(cache_dir).resolve()
        elif is_testing:
            import tempfile
            self.cache_dir = Path(tempfile.mkdtemp(prefix="pytest_http_cache_"))
        else:
            self.cache_dir = Path(__file__).resolve().parent.parent / "data" / "http_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # In-run counters and telemetry
        self.requests_this_run = 0
        self.counts_by_layer = {"json": 0, "rss": 0, "praw": 0, "about": 0}
        self.count_403 = 0
        self.count_429 = 0
        self.consecutive_stop_signals = {"json": 0, "rss": 0, "praw": 0, "about": 0}
        self.open_layers_this_run = set()
        self._last_request_time = 0.0

    def _wait_spacing(self) -> None:
        """Enforce per-request spacing across all layers."""
        if self.min_delay <= 0.0 and self.max_delay <= 0.0:
            return
        now = time.time()
        elapsed = now - self._last_request_time
        target_delay = random.uniform(self.min_delay, self.max_delay)
        if elapsed < target_delay:
            sleep_time = target_delay - elapsed
            logger.debug(f"Reddit pacing: sleeping {sleep_time:.2f}s (target delay {target_delay:.2f}s)")
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def check_circuit_breaker(
        self,
        layer: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if circuit breaker is open for a layer (or across all layers if layer is None).
        Returns (is_blocked, blocked_until_ist_string_or_reason).
        """
        if force:
            return False, None

        now_utc = datetime.now(timezone.utc)

        # Check DB persistent breaker
        all_breakers = self.db.get_all_breakers()

        def is_breaker_active(info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
            blocked_until_str = info.get("blocked_until")
            if not blocked_until_str:
                return False, None
            try:
                blocked_dt = datetime.fromisoformat(blocked_until_str)
                if blocked_dt.tzinfo is None:
                    blocked_dt = blocked_dt.replace(tzinfo=timezone.utc)
                if blocked_dt > now_utc:
                    ist_dt = blocked_dt.astimezone(IST_TZ)
                    ist_fmt = ist_dt.strftime("%I:%M %p IST").lstrip("0")
                    return True, f"blocked_until {ist_fmt}"
            except Exception as e:
                logger.debug(f"Error parsing blocked_until timestamp '{blocked_until_str}': {e}")
            return False, None

        # In-run open check for specific layer
        if layer and layer in self.open_layers_this_run:
            info = all_breakers.get(layer) or self.db.get_breaker(layer)
            if info:
                _, reason = is_breaker_active(info)
                if reason:
                    return True, reason
            return True, f"{layer} layer open for this run"

        if layer:
            info = all_breakers.get(layer)
            if info:
                blocked, reason = is_breaker_active(info)
                if blocked:
                    return True, reason
            return False, None

        # Check if all primary feeds ("json", "rss", "praw") are blocked
        primary_layers = ["json", "rss"]
        if os.getenv("REDDIT_CLIENT_ID"):
            primary_layers.append("praw")

        blocked_count = 0
        latest_reason = None
        for l in primary_layers:
            if l in self.open_layers_this_run:
                blocked_count += 1
                continue
            info = all_breakers.get(l)
            if info:
                blocked, reason = is_breaker_active(info)
                if blocked:
                    blocked_count += 1
                    latest_reason = reason

        if blocked_count >= len(primary_layers):
            return True, latest_reason or "all layers blocked"

        return False, None

    def can_request(self, layer: str = "json") -> Tuple[bool, str]:
        """Verify request budget limits before initiating an outbound request."""
        if self.requests_this_run >= self.max_requests_per_run:
            msg = f"budget reached for this run ({self.requests_this_run}/{self.max_requests_per_run})"
            logger.warning(f"Reddit request refused: {msg}")
            return False, msg

        today_ist = get_ist_today()
        today_count = self.db.get_requests_count_today(today_ist, host="reddit")
        if today_count >= self.max_requests_per_day:
            msg = f"daily budget reached ({today_count}/{self.max_requests_per_day})"
            logger.warning(f"Reddit request refused: {msg}")
            return False, msg

        return True, "ok"

    def record_external_request(self, layer: str, status_code: Optional[int] = 200) -> None:
        """Allow counting non-HTTP requests (e.g. PRAW calls) against the budget."""
        self.requests_this_run += 1
        self.counts_by_layer[layer] = self.counts_by_layer.get(layer, 0) + 1
        today_ist = get_ist_today()
        self.db.log_request(today_ist, host="reddit", kind=layer, status_code=status_code)

    def _get_cache_path(self, url: str, params: Optional[Dict[str, Any]] = None) -> Path:
        """Compute unique deterministic cache file path."""
        norm_params = sorted((k, str(v)) for k, v in (params or {}).items())
        key_str = f"{url}?{norm_params}"
        hash_key = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{hash_key}.json"

    def _read_cache(
        self,
        cache_path: Path,
        ttl_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        """Read fresh cache entry if available."""
        if not cache_path.is_file():
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at < ttl_seconds:
                return data
            return data  # Return expired entry so caller can send conditional 304 headers
        except Exception as e:
            logger.debug(f"Cache read error for {cache_path}: {e}")
            return None

    def _write_cache(
        self,
        cache_path: Path,
        url: str,
        params: Optional[Dict[str, Any]],
        status_code: int,
        headers: Dict[str, str],
        body_text: str,
    ) -> None:
        """Persist HTTP response to on-disk cache."""
        try:
            cache_obj = {
                "url": url,
                "params": params or {},
                "status_code": status_code,
                "cached_at": time.time(),
                "etag": headers.get("etag") or headers.get("ETag"),
                "last_modified": headers.get("last-modified") or headers.get("Last-Modified"),
                "body": body_text,
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_obj, f)
        except Exception as e:
            logger.debug(f"Failed to write cache for {url}: {e}")

    def _trip_breaker_for_layer(self, layer: str, reason_status: int) -> None:
        """Advance backoff step and record breaker activation in database."""
        self.open_layers_this_run.add(layer)
        info = self.db.get_breaker(layer) or {}
        prev_failures = info.get("consecutive_failures", 0)
        new_failures = prev_failures + 1

        backoff_idx = min(new_failures - 1, len(BACKOFF_STEPS) - 1)
        backoff_duration = BACKOFF_STEPS[backoff_idx]

        blocked_until = (datetime.now(timezone.utc) + timedelta(seconds=backoff_duration)).isoformat()
        self.db.update_breaker(layer, blocked_until, new_failures, f"error_{reason_status}")

        ist_dt = datetime.fromisoformat(blocked_until).astimezone(IST_TZ)
        ist_fmt = ist_dt.strftime("%I:%M %p IST").lstrip("0")
        logger.warning(
            f"Circuit breaker tripped for Reddit layer '{layer}' after {new_failures} consecutive failure(s). "
            f"Blocked until {ist_fmt} (backoff {backoff_duration // 60} min)."
        )

    def _reset_breaker_for_layer(self, layer: str) -> None:
        """Clear failures and reset breaker status upon successful response."""
        self.consecutive_stop_signals[layer] = 0
        self.db.reset_breaker(layer)

    def request(
        self,
        url: str,
        layer: str = "json",
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
        force: bool = False,
        use_cache: bool = True,
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[Optional[httpx.Response], str]:
        """
        Execute rate-limited, cached, breaker-protected HTTP request to Reddit.
        Returns (response_or_mock_response, status_flag).
        """
        # 1. Circuit breaker check
        is_blocked, block_reason = self.check_circuit_breaker(layer=layer, force=force)
        if is_blocked:
            logger.info(f"Reddit request to {url} skipped: {block_reason}")
            return None, block_reason or "breaker_blocked"

        # Determine TTL based on URL/listing
        if ttl_seconds is None:
            if "top_week" in url or (params and params.get("t") == "week"):
                ttl_seconds = 43200  # 12 hours
            else:
                ttl_seconds = 10800  # 3 hours

        cache_path = self._get_cache_path(url, params)
        cached_entry = self._read_cache(cache_path, ttl_seconds) if use_cache else None

        # Fresh cache hit: 0 budget, 0 delay
        if cached_entry and (time.time() - cached_entry.get("cached_at", 0) < ttl_seconds):
            logger.debug(f"Reddit cache hit for {url}")
            mock_resp = httpx.Response(
                status_code=cached_entry.get("status_code", 200),
                content=cached_entry.get("body", "").encode("utf-8"),
                headers={"X-Cache": "HIT"},
                request=httpx.Request("GET", url),
            )
            return mock_resp, "cache_hit"

        # 2. Request budget check
        can_proceed, budget_reason = self.can_request(layer=layer)
        if not can_proceed:
            return None, budget_reason

        # 3. Pacing spacing
        self._wait_spacing()

        # 4. Request headers preparation (Honest UA, no fake browser)
        req_headers = {
            "User-Agent": get_reddit_user_agent(),
            "Accept": "application/json" if ".json" in url else "application/xml, text/xml, */*",
        }
        if headers:
            req_headers.update(headers)

        # Conditional headers from expired cache if available
        if cached_entry:
            if cached_entry.get("etag"):
                req_headers["If-None-Match"] = cached_entry["etag"]
            if cached_entry.get("last_modified"):
                req_headers["If-Modified-Since"] = cached_entry["last_modified"]

        # 5. Outbound request execution
        self.requests_this_run += 1
        self.counts_by_layer[layer] = self.counts_by_layer.get(layer, 0) + 1
        today_ist = get_ist_today()

        try:
            resp = self.http.get(url, params=params, headers=req_headers, timeout=timeout)
        except Exception as exc:
            self.db.log_request(today_ist, host="reddit", kind=layer, status_code=0)
            logger.warning(f"Reddit transport exception on {url}: {exc}")
            resp = None
            resp_status = 0

        if resp is not None:
            resp_status = getattr(resp, "status_code", 0)
            self.db.log_request(today_ist, host="reddit", kind=layer, status_code=resp_status)

        # 6. Handle 304 Not Modified
        if resp_status == 304 and cached_entry:
            logger.debug(f"Reddit 304 Not Modified for {url}; refreshed cache entry.")
            cached_entry["cached_at"] = time.time()
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cached_entry, f)
            except Exception:
                pass
            mock_resp = httpx.Response(
                status_code=200,
                content=cached_entry.get("body", "").encode("utf-8"),
                headers={"X-Cache": "304_HIT"},
                request=httpx.Request("GET", url),
            )
            return mock_resp, "cache_304"

        resp_headers = getattr(resp, "headers", {})
        if not hasattr(resp_headers, "get"):
            resp_headers = {}

        # 7. Check for Stop Signals (403, 404, 429, HTML block page, ratelimit remaining <= 0)
        is_stop_signal = False
        stop_reason = "ok"

        if resp is None:
            is_stop_signal = True
            stop_reason = "empty_or_failed"
        elif resp_status in (403, 429, 404):
            is_stop_signal = True
            if resp_status == 403:
                self.count_403 += 1
                stop_reason = "403_forbidden"
                logger.warning(f"Reddit 403 received for {url} ({layer}).")
            elif resp_status == 429:
                self.count_429 += 1
                stop_reason = "429_rate_limited"
                logger.warning(f"Reddit 429 rate limit received for {url} ({layer}).")
            else:
                stop_reason = f"{resp_status}_error"
        else:
            # Check for HTML block page when JSON expected
            raw_text = getattr(resp, "text", "")
            text_preview = raw_text[:200].lower() if isinstance(raw_text, str) else ""
            if (".json" in url or layer == "json") and ("<!doctype html" in text_preview or "<html" in text_preview):
                is_stop_signal = True
                stop_reason = "html_block_page"
                logger.warning(f"Reddit returned HTML block page instead of JSON for {url}.")

            # Check x-ratelimit-remaining == 0
            rem = resp_headers.get("x-ratelimit-remaining")
            if rem is not None:
                try:
                    if float(rem) <= 0.0:
                        is_stop_signal = True
                        stop_reason = "ratelimit_remaining_exhausted"
                except ValueError:
                    pass

        # 8. Stop signal & 429 handling with immediate persistence
        if is_stop_signal:
            self.consecutive_stop_signals[layer] = self.consecutive_stop_signals.get(layer, 0) + 1
            logger.info(
                f"Reddit stop signal detected on '{layer}' ({stop_reason}). "
                f"Consecutive stop signals in run: {self.consecutive_stop_signals[layer]}"
            )

            # If 2 consecutive stop signals, trip the breaker immediately and persist to DB!
            if self.consecutive_stop_signals[layer] >= 2:
                self._trip_breaker_for_layer(layer, resp_status or 400)
                return resp, stop_reason

            # If this is the FIRST 429, we can attempt a single polite retry after Retry-After sleep
            if resp_status == 429:
                raw_retry = resp_headers.get("Retry-After") or resp_headers.get("x-ratelimit-reset")
                try:
                    retry_wait = float(raw_retry) if raw_retry else 30.0
                    retry_wait = min(max(retry_wait, 5.0), 120.0)
                except (ValueError, TypeError):
                    retry_wait = 30.0

                logger.warning(
                    f"Reddit 429 rate limit on {url}. Retry header specifies {raw_retry}s. "
                    f"Waiting {retry_wait:.0f}s (capped at 120s) before single retry..."
                )
                time.sleep(retry_wait)

                self.requests_this_run += 1
                self.counts_by_layer[layer] = self.counts_by_layer.get(layer, 0) + 1
                try:
                    resp = self.http.get(url, params=params, headers=req_headers, timeout=timeout)
                    resp_status = getattr(resp, "status_code", 0) if resp else 0
                    self.db.log_request(today_ist, host="reddit", kind=layer, status_code=resp_status)
                except Exception as e:
                    self.db.log_request(today_ist, host="reddit", kind=layer, status_code=0)
                    logger.warning(f"Reddit retry error on {url}: {e}")
                    resp = None
                    resp_status = 0

                # Check retry outcome
                if resp is None or resp_status in (403, 429, 404):
                    self.consecutive_stop_signals[layer] += 1
                    logger.warning(f"Reddit retry failed with {resp_status}; tripping breaker for '{layer}'.")
                    self._trip_breaker_for_layer(layer, resp_status or 429)
                    return resp, f"retry_failed_{resp_status}"
                elif resp_status == 200:
                    self._reset_breaker_for_layer(layer)
                    return resp, "ok"

            return resp, stop_reason

        # 9. Successful response: reset consecutive errors for layer and write cache
        self._reset_breaker_for_layer(layer)
        if resp_status == 200 and use_cache:
            safe_headers = {}
            if isinstance(resp_headers, dict):
                safe_headers = resp_headers
            elif hasattr(resp_headers, "items"):
                try:
                    safe_headers = {str(k): str(v) for k, v in resp_headers.items()}
                except Exception:
                    pass
            safe_text = getattr(resp, "text", "")
            if isinstance(safe_text, str):
                self._write_cache(
                    cache_path,
                    url,
                    params,
                    resp_status,
                    safe_headers,
                    safe_text,
                )

        return resp, "ok"

    def get_summary_line(self) -> str:
        """
        Produce a concise single-line summary of Reddit activity:
        e.g. 'Reddit: 18 requests (json 12, rss 4, praw 0, about 2), 403: 2, 429: 0, breaker: json open until 10:30 IST'
        """
        j = self.counts_by_layer.get("json", 0)
        r = self.counts_by_layer.get("rss", 0)
        p = self.counts_by_layer.get("praw", 0)
        a = self.counts_by_layer.get("about", 0)
        tot = self.requests_this_run

        breaker_str = "closed"
        all_breakers = self.db.get_all_breakers()
        now_utc = datetime.now(timezone.utc)
        open_msgs = []
        for l, info in all_breakers.items():
            b_str = info.get("blocked_until")
            if b_str:
                try:
                    b_dt = datetime.fromisoformat(b_str)
                    if b_dt.tzinfo is None:
                        b_dt = b_dt.replace(tzinfo=timezone.utc)
                    if b_dt > now_utc:
                        ist_time = b_dt.astimezone(IST_TZ).strftime("%I:%M %p IST").lstrip("0")
                        open_msgs.append(f"{l} open until {ist_time}")
                except Exception:
                    pass

        for l in self.open_layers_this_run:
            if not any(l in m for m in open_msgs):
                open_msgs.append(f"{l} open this run")

        if open_msgs:
            breaker_str = "; ".join(open_msgs)

        return (
            f"Reddit: {tot} requests (json {j}, rss {r}, praw {p}, about {a}), "
            f"403: {self.count_403}, 429: {self.count_429}, breaker: {breaker_str}"
        )
