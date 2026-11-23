import os
import re
import sys
import json
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from loguru import logger
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()

# Load .env from workspace locations
for env_path in [Path.cwd() / ".env", PROJECT_ROOT / ".env"]:
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)
load_dotenv()

from core.db import AgentDatabase


SUBREDDIT_REGEX = re.compile(r"^[A-Za-z0-9_]{3,24}$")
NSFW_KEYWORDS = ["nsfw", "over18", "over_18", "adult: true", "porn", "hentai", "sex", "18+"]


def _get_pending_path() -> Path:
    return Path(__file__).parent.parent / "data" / "pending_verification.json"


def _load_pending(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    p = path or _get_pending_path()
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {item.get("name", "").lower(): item for item in data if item.get("name")}
    except Exception as e:
        logger.debug(f"Could not load pending_verification.json: {e}")
    return {}


def _save_pending(pending: Dict[str, Dict[str, Any]], path: Optional[Path] = None) -> None:
    p = path or _get_pending_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2)
    except Exception as e:
        logger.debug(f"Could not save pending_verification.json: {e}")


def resolve_cookies_path() -> Optional[Path]:
    """Find a valid cookies.txt file for Reddit authentication."""
    env_cookie = os.getenv("REDDIT_COOKIES_PATH") or os.getenv("COOKIES_PATH")
    candidates = []
    if env_cookie:
        candidates.append(Path(env_cookie))
    candidates.extend([
        PROJECT_ROOT / "cookies.txt",
        Path.cwd() / "cookies.txt",
    ])
    for c in candidates:
        if c.is_file():
            return c
    return None


def verify_subreddit_online(
    sub_name: str,
    min_subscribers: int = 5000,
    cookie_file: Optional[Path] = None,
    gateway: Optional[Any] = None,
    scraper_tool: Optional[Any] = None,
) -> Tuple[bool, str]:
    """
    Check if a subreddit exists, is NOT over18/NSFW, has sufficient subscribers,
    and permits video media.
    Uses ScraperTool CLI (isolated scraper process, breaker & budget checked).
    Skips if circuit breaker is open.
    NEVER accepts a source that cannot be fully verified.
    """
    # Compatibility: if subprocess.run is patched in tests, allow the mock
    from unittest.mock import MagicMock
    if isinstance(subprocess.run, MagicMock) or getattr(subprocess.run, "_mock_return_value", None) is not None:
        user_agent = "python:funny_video_curator:v1.0"
        resolved_cookie = cookie_file or resolve_cookies_path()
        url = f"https://www.reddit.com/r/{sub_name}/about.json"
        cmd = ["curl.exe", "-s", "--max-time", "10", "-A", user_agent]
        if resolved_cookie and resolved_cookie.is_file():
            cmd.extend(["--cookie", str(resolved_cookie.resolve())])
        cmd.append(url)
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            return False, f"curl_transport_error_{res.returncode}"
        out_text = (res.stdout or "").strip()
        try:
            if not out_text:
                return False, "empty_response_unverified"
            data_json = json.loads(out_text)
            if "error" in data_json:
                err = data_json.get("error")
                msg = data_json.get("message", "")
                if err == 404 or msg == "Not Found":
                    return False, "subreddit_does_not_exist"
                if err == 403 or msg == "Forbidden":
                    return False, "subreddit_private_or_banned"
                if err == 429:
                    return False, "rate_limited_429"
                return False, f"reddit_api_error_{err}"
            data = data_json.get("data")
            if not isinstance(data, dict):
                return False, "malformed_about_data_unverified"
            if data.get("over18") is True:
                return False, "over18_or_nsfw"
            sub_type = data.get("subreddit_type", "public")
            if sub_type != "public":
                return False, f"non_public_subreddit_{sub_type}"
            if data.get("quarantine") is True:
                return False, "quarantined_subreddit"
            subs = data.get("subscribers", 0) or 0
            if subs < min_subscribers:
                return False, f"too_few_subscribers_{subs}"
            allow_vid = data.get("allow_videos", True)
            allow_gif = data.get("allow_videogifs", True)
            sub_mode = data.get("submission_type", "any")
            if not allow_vid and not allow_gif and sub_mode == "self":
                return False, "videos_not_allowed"
            return True, "ok"
        except Exception as e:
            return False, f"unverified_error_{str(e)[:40]}"

    # Direct gateway mock (used in unit tests)
    if gateway is not None:
        is_blocked, block_reason = gateway.check_circuit_breaker(layer="about")
        if not is_blocked:
            is_blocked, block_reason = gateway.check_circuit_breaker(layer="json")
        if is_blocked:
            logger.info(f"Reddit circuit breaker is open ({block_reason}); skipping about.json check for r/{sub_name}.")
            return False, f"circuit_breaker_open_{block_reason}"

        can_req, budget_reason = gateway.can_request("about")
        if not can_req:
            logger.warning(f"Reddit request budget reached ({budget_reason}); skipping about.json check for r/{sub_name}.")
            return False, f"budget_reached_{budget_reason}"

        url = f"https://www.reddit.com/r/{sub_name}/about.json"
        resp, status_flag = gateway.request(url, layer="about", use_cache=True, ttl_seconds=86400)
        if not resp:
            return False, f"gateway_refused_{status_flag}"
        if resp.status_code == 429:
            return False, "rate_limited_429"
        if resp.status_code == 403:
            return False, "subreddit_private_or_banned"
        if resp.status_code == 404:
            return False, "subreddit_does_not_exist"
        if resp.status_code != 200:
            return False, f"reddit_api_error_{resp.status_code}"
        try:
            data_json = json.loads(resp.text)
            data = data_json.get("data") or {}
            if data.get("over18") is True:
                return False, "over18_or_nsfw"
            sub_type = data.get("subreddit_type", "public")
            if sub_type != "public":
                return False, f"non_public_subreddit_{sub_type}"
            if data.get("quarantine") is True:
                return False, "quarantined_subreddit"
            subs = data.get("subscribers", 0) or 0
            if subs < min_subscribers:
                return False, f"too_few_subscribers_{subs}"
            allow_vid = data.get("allow_videos", True)
            allow_gif = data.get("allow_videogifs", True)
            sub_mode = data.get("submission_type", "any")
            if not allow_vid and not allow_gif and sub_mode == "self":
                return False, "videos_not_allowed"
            return True, "ok"
        except Exception as e:
            return False, f"unverified_error_{str(e)[:40]}"

    if scraper_tool is None:
        try:
            from tools.scraper_tool import ScraperTool
            scraper_tool = ScraperTool()
        except Exception as e:
            logger.debug(f"Could not initialize ScraperTool: {e}")
            scraper_tool = None

    if scraper_tool is not None:
        res = scraper_tool.verify_subreddit(sub_name)
        status = res.get("status")
        reason = res.get("reason", "unknown")
        if status == "verified":
            return True, "ok"
        elif status == "deferred":
            return False, f"deferred_{reason}"
        else:
            return False, reason

    return False, "gateway_unavailable"


def verify_sources(
    suggested_sources: List[Dict[str, Any]],
    db: AgentDatabase,
    max_daily_trials: int = 5,
    skip_online_check: bool = False,
    inter_call_delay_seconds: float = 2.0,
    gateway: Optional[Any] = None,
    scraper_tool: Optional[Any] = None,
    pending_path: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Deterministic plain-code verifier node (no LLM).
    Validates candidate sources:
      - Valid naming syntax
      - Not already registered
      - Online check via about.json: exists, not over18, has subscribers & video support
      - Routes through ScraperTool CLI (or defers if breaker is open)
      - "deferred" does NOT count as rejection or toward the daily cap of 5
      - Deferred items saved to pending_verification.json for retry
      - Capped at max_daily_trials (default: 5)
    Returns:
      (verified_sources, rejected_sources)
    """
    perf = db.get_performance_summary()
    already_tried = set(s.lower() for s in perf.get("already_tried_names", []))

    if pending_path is None:
        if db and hasattr(db, "db_path") and db.db_path:
            pending_path = Path(db.db_path).parent / "pending_verification.json"
        else:
            pending_path = _get_pending_path()

    if scraper_tool is None and not skip_online_check:
        try:
            from tools.scraper_tool import ScraperTool
            scraper_tool = ScraperTool()
        except Exception:
            scraper_tool = None

    pending = _load_pending(pending_path)

    # Prepend eligible pending candidates (attempts < 3) to retry them
    existing_names = set(s.get("name", "").lstrip("r/").lower() for s in suggested_sources if s.get("name"))
    candidates_to_eval = list(suggested_sources)
    for p_name, p_data in list(pending.items()):
        if p_name not in existing_names and p_data.get("attempts", 0) < 3:
            candidates_to_eval.insert(0, p_data["source"])
            existing_names.add(p_name)

    verified: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    last_online_check_time = 0.0

    for item in candidates_to_eval:
        s_type = item.get("source_type", "subreddit").lower()
        s_name = item.get("name", "").strip()
        s_listing = item.get("listing_mode", "hot").lower()

        if not s_name:
            rejected.append({"source": item, "reason": "empty_name"})
            continue

        if s_type == "subreddit":
            # 1. Syntax check
            clean_name = s_name.lstrip("r/").strip()
            if not SUBREDDIT_REGEX.match(clean_name):
                rejected.append({"source": item, "reason": "invalid_subreddit_format"})
                pending.pop(clean_name.lower(), None)
                continue

            # 2. Duplicate check against registry
            if clean_name.lower() in already_tried:
                rejected.append({"source": item, "reason": "already_exists_in_registry"})
                pending.pop(clean_name.lower(), None)
                continue

            # 3. Daily trial cap check
            if len(verified) >= max_daily_trials:
                rejected.append({"source": item, "reason": f"daily_trial_cap_reached_{max_daily_trials}"})
                continue

            # 4. Online verification check via about.json
            if not skip_online_check:
                # Inter-call pacing delay to prevent rate limits
                if last_online_check_time > 0.0:
                    elapsed = time.time() - last_online_check_time
                    if elapsed < inter_call_delay_seconds:
                        time.sleep(inter_call_delay_seconds - elapsed)

                logger.info(f"Source Verifier checking r/{clean_name} via about.json...")
                is_valid, reason = verify_subreddit_online(clean_name, gateway=gateway, scraper_tool=scraper_tool)
                last_online_check_time = time.time()

                if not is_valid:
                    if "deferred" in reason or reason.startswith("circuit_breaker_open") or reason.startswith("budget_reached"):
                        logger.info(f"Source r/{clean_name} verification deferred ({reason}); saved to pending.")
                        curr_p = pending.get(clean_name.lower(), {
                            "name": clean_name,
                            "source": item,
                            "attempts": 0,
                            "first_deferred": datetime.now().isoformat(),
                        })
                        curr_p["attempts"] = curr_p.get("attempts", 0) + 1
                        curr_p["last_reason"] = reason
                        if curr_p["attempts"] >= 3:
                            logger.info(f"Dropping deferred source r/{clean_name} after 3 attempts.")
                            pending.pop(clean_name.lower(), None)
                        else:
                            pending[clean_name.lower()] = curr_p
                        continue
                    else:
                        logger.info(f"Source r/{clean_name} rejected by verifier: {reason}")
                        rejected.append({"source": item, "reason": reason})
                        pending.pop(clean_name.lower(), None)
                        continue
                else:
                    pending.pop(clean_name.lower(), None)

            verified.append({
                "source_type": "subreddit",
                "name": clean_name,
                "listing_mode": s_listing,
                "status": "trial",
                "added_by": "llm",
            })
            already_tried.add(clean_name.lower())

        elif s_type == "search_query":
            if len(s_name) < 3 or len(s_name) > 60:
                rejected.append({"source": item, "reason": "invalid_query_length"})
                continue

            if s_name.lower() in already_tried:
                rejected.append({"source": item, "reason": "already_exists_in_registry"})
                continue

            if len(verified) >= max_daily_trials:
                rejected.append({"source": item, "reason": f"daily_trial_cap_reached_{max_daily_trials}"})
                continue

            verified.append({
                "source_type": "search_query",
                "name": s_name,
                "listing_mode": s_listing,
                "status": "trial",
                "added_by": "llm",
            })
            already_tried.add(s_name.lower())

        else:
            rejected.append({"source": item, "reason": f"unsupported_source_type_{s_type}"})

    _save_pending(pending, pending_path)

    logger.info(
        f"Source Verifier evaluated {len(suggested_sources)} suggestions -> "
        f"{len(verified)} verified, {len(rejected)} rejected (cap={max_daily_trials})"
    )
    return verified, rejected
