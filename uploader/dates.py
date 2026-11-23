"""
Shared date resolution utilities for 100B Video Bot.

Timezone Design:
- Scheduling and channel broadcasting remain in America/New_York (ET) and UTC.
- Daily bot runs and human dashboard display operate in Asia/Kolkata (IST).
- Date keyword 'ist-today' resolves to the current calendar date in Asia/Kolkata (YYYY-MM-DD).
- Date keywords 'today' and 'tomorrow' resolve relative to the channel timezone (America/New_York).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional


def get_ist_today(now: Optional[datetime] = None) -> str:
    """Return today's calendar date in Asia/Kolkata as 'YYYY-MM-DD'."""
    tz_ist = ZoneInfo("Asia/Kolkata")
    if now is not None:
        if now.tzinfo is None:
            dt = now.replace(tzinfo=tz_ist)
        else:
            dt = now.astimezone(tz_ist)
    else:
        dt = datetime.now(tz_ist)
    return dt.strftime("%Y-%m-%d")


def resolve_date_arg(
    date_str: Optional[str],
    timezone_name: str = "America/New_York",
    now: Optional[datetime] = None,
) -> str:
    """
    Resolve date argument into a concrete 'YYYY-MM-DD' string.

    Supported values:
    - 'ist-today', 'ist_today': current calendar date in Asia/Kolkata
    - 'tomorrow', 'next_day': tomorrow in channel timezone (America/New_York)
    - 'today', 'now': today in channel timezone (America/New_York)
    - 'yesterday', 'prev', 'prev_day': yesterday in channel timezone (America/New_York)
    - 'YYYY-MM-DD': validated explicit date string
    """
    s = (date_str or "tomorrow").strip().lower()

    # 1. IST Today keyword
    if s in ("ist-today", "ist_today"):
        return get_ist_today(now=now)

    # Resolve relative to channel timezone
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc

    if now is not None:
        if now.tzinfo is None:
            current_dt = now.replace(tzinfo=tz)
        else:
            current_dt = now.astimezone(tz)
    else:
        current_dt = datetime.now(tz)

    # 2. Relative keywords
    if s in ("tomorrow", "next_day"):
        return (current_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    elif s in ("today", "now"):
        return current_dt.strftime("%Y-%m-%d")
    elif s in ("yesterday", "prev", "prev_day"):
        return (current_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    # 3. Explicit YYYY-MM-DD
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return (current_dt + timedelta(days=1)).strftime("%Y-%m-%d")
