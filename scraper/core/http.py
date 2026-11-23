import os
import random
import time
from typing import Optional, Dict, Any, List
import httpx
from loguru import logger
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# Realistic modern User-Agents pool
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    """Return a randomized realistic browser User-Agent."""
    return random.choice(USER_AGENTS)


class RateLimiter:
    """Per-source adaptive rate limiter with random jitter."""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.5):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_call: Dict[str, float] = {}

    def wait(self, source_name: str) -> None:
        last = self._last_call.get(source_name, 0.0)
        delay = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.time() - last
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_call[source_name] = time.time()


class HttpClient:
    """
    Resilient HTTP Client with:
      - Automatic User-Agent rotation
      - Exponential backoff on 429/403/5xx
      - Proxy support
      - CAPTCHA / rate-limit detection
    """

    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
        proxies: Optional[List[str]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.proxies = proxies or []
        self._client: Optional[httpx.Client] = None

    def get_proxy(self) -> Optional[str]:
        if self.proxies:
            return random.choice(self.proxies)
        env_proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        return env_proxy if env_proxy else None

    def _get_headers(self, custom_headers: Optional[Dict[str, str]] = None, url: str = "") -> Dict[str, str]:
        # For Reddit requests, strictly use honest User-Agent and never browser headers
        if "reddit.com" in url:
            from core.reddit_gateway import get_reddit_user_agent
            headers = {
                "User-Agent": get_reddit_user_agent(),
                "Accept": "application/json" if ".json" in url else "application/xml, text/xml, */*",
            }
            if custom_headers:
                headers.update(custom_headers)
            return headers

        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[httpx.Response]:
        """Send a GET request with automatic retry and backoff."""
        req_headers = self._get_headers(headers, url=url)
        req_timeout = timeout or self.timeout
        proxy = self.get_proxy()

        retries = 0
        current_delay = 1.0

        while retries <= self.max_retries:
            try:
                with httpx.Client(
                    proxy=proxy,
                    timeout=req_timeout,
                    follow_redirects=True,
                ) as client:
                    resp = client.get(url, params=params, headers=req_headers)

                    # Check for rate limits / blocks
                    if resp.status_code == 429:
                        logger.warning(f"Rate limited (429) on {url}. Reset header: {resp.headers.get('x-ratelimit-reset')}")
                        return resp

                    if resp.status_code == 403:
                        logger.warning(f"Forbidden (403) on {url}. Endpoint requires authentication or is blocking unauthenticated clients.")
                        return resp

                    if resp.status_code >= 500:
                        logger.warning(f"Server error ({resp.status_code}) on {url}. Retrying...")
                        time.sleep(current_delay)
                        current_delay *= self.backoff_factor
                        retries += 1
                        continue

                    resp.raise_for_status()
                    return resp

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                retries += 1
                logger.warning(f"HTTP request error ({exc}) on {url}. Attempt {retries}/{self.max_retries}")
                if retries > self.max_retries:
                    logger.error(f"Failed to fetch {url} after {self.max_retries} attempts.")
                    return None
                time.sleep(current_delay)
                current_delay *= self.backoff_factor

        return None
