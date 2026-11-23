from datetime import datetime
from zoneinfo import ZoneInfo
from uploader.dates import resolve_date_arg, get_ist_today


def test_ist_today_at_10_13_ist():
    # 2026-11-20 10:13 IST -> 2026-11-20
    tz_ist = ZoneInfo("Asia/Kolkata")
    frozen_ist = datetime(2026, 11, 20, 10, 13, 0, tzinfo=tz_ist)
    assert get_ist_today(now=frozen_ist) == "2026-11-20"
    assert resolve_date_arg("ist-today", now=frozen_ist) == "2026-11-20"
    assert resolve_date_arg("ist_today", now=frozen_ist) == "2026-11-20"


def test_ist_today_at_00_30_ist_still_19_nov_in_et():
    # At 2026-11-20 00:30 IST, in America/New_York (EST, UTC-5) it is 2026-11-19 14:00 (still 19 Nov)
    tz_ist = ZoneInfo("Asia/Kolkata")
    frozen_ist = datetime(2026, 11, 20, 0, 30, 0, tzinfo=tz_ist)

    # ist-today resolves strictly to 2026-11-20
    assert resolve_date_arg("ist-today", now=frozen_ist) == "2026-11-20"

    # In contrast, ET-relative "today" remains 2026-11-19
    assert resolve_date_arg("today", timezone_name="America/New_York", now=frozen_ist) == "2026-11-19"
    # And ET-relative "tomorrow" resolves to 2026-11-20
    assert resolve_date_arg("tomorrow", timezone_name="America/New_York", now=frozen_ist) == "2026-11-20"


def test_ist_today_at_23_50_ist():
    # 2026-11-20 23:50 IST -> 2026-11-20
    tz_ist = ZoneInfo("Asia/Kolkata")
    frozen_ist = datetime(2026, 11, 20, 23, 50, 0, tzinfo=tz_ist)
    assert resolve_date_arg("ist-today", now=frozen_ist) == "2026-11-20"


def test_ist_today_summer_date_edt():
    # Summer date (EDT, UTC-4): 2027-06-20 00:30 IST is 2027-06-19 15:00 EDT
    tz_ist = ZoneInfo("Asia/Kolkata")
    frozen_summer = datetime(2027, 6, 20, 0, 30, 0, tzinfo=tz_ist)

    assert resolve_date_arg("ist-today", now=frozen_summer) == "2027-06-20"
    assert resolve_date_arg("today", timezone_name="America/New_York", now=frozen_summer) == "2027-06-19"
    assert resolve_date_arg("tomorrow", timezone_name="America/New_York", now=frozen_summer) == "2027-06-20"


def test_today_and_tomorrow_et_unchanged():
    # Test that ET-relative behavior is preserved exactly as before
    tz_et = ZoneInfo("America/New_York")
    now_et = datetime(2026, 11, 20, 12, 0, 0, tzinfo=tz_et)

    assert resolve_date_arg("today", timezone_name="America/New_York", now=now_et) == "2026-11-20"
    assert resolve_date_arg("tomorrow", timezone_name="America/New_York", now=now_et) == "2026-11-21"
    assert resolve_date_arg("yesterday", timezone_name="America/New_York", now=now_et) == "2026-11-19"
    assert resolve_date_arg("2026-12-25", timezone_name="America/New_York", now=now_et) == "2026-12-25"
