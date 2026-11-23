"""
agent/tests/test_claims_sync.py

Comprehensive test suite for Phase 6.1:
  1. Sanitized fixtures for all 6 scenarios:
     - Claim added (Fortunate Son / CCR / Delivery Driver clip)
     - Claim released (car clip)
     - Visual claim (paramount footage)
     - Copyright strike email
     - Older legacy channel video not in uploads table (no action)
     - Duplicates / Idempotency
  2. Acceptance Criteria 1, 2, 3 verification
  3. Safety checks (dry-run simulation, strike escalation, daily cap)
  4. YouTube Studio paste parsing
"""

import os
import sys
import json
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure paths
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_DIR = WORKSPACE_ROOT / "agent"
for p in [str(AGENT_DIR), str(WORKSPACE_ROOT)]:
    if p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(AGENT_DIR))

if "core" in sys.modules and not hasattr(sys.modules["core"], "db"):
    for key in list(sys.modules.keys()):
        if key == "core" or key.startswith("core."):
            del sys.modules[key]

from core.db import AgentDatabase
from claims_sync.parser import parse_claim_email, parse_studio_paste, ParsedClaimEmail
from claims_sync.matcher import ClaimsMatcher, normalize_title_for_matching
from claims_sync.actions import execute_claim_action, undo_auto_hold, log_claim_action
from claims_sync.sync import probe_missing_videos_on_youtube
from claims_sync.config import load_claims_config, set_dry_run


# ==============================================================================
# FIXTURES
# ==============================================================================

FIXTURE_CLAIM_ADDED_FORTUNATE_SON = {
    "message_id": "msg_fortunate_son_001",
    "subject": "Video cannot be monetised: Delivery Driver Finds Secret Cheat Code For Dogs #shorts #funny",
    "sender": "YouTube <no-reply@youtube.com>",
    "date": "Tue, 17 Nov 2026 04:47:29 -0800",
    "snippet": "Hi Betterverse, Claimed content was found in your video 'Delivery Driver Finds Secret Cheat Code For Dogs #shorts #funny'. The visibility of your video is not impacted",
    "body_text": """
Hi Betterverse,

Claimed content was found in your video 'Delivery Driver Finds Secret Cheat  
Code For Dogs #shorts #funny'. The visibility of your video is not  
impacted, but you can no longer monetise it. This is not a  
copyright strike and does not affect your channel.

CONTENT USED
Fortunate Son - Creedence Clearwater Revival

Content owners:  UMG
Type:  Audio
Impact on video:  No impact until you become a monetising creator

View all actions
<https://studio.youtube.com/video/HcwZS5Sxv_Y/edit?d=vc>
""",
    "body_html": """
<div>
  <p>Hi Betterverse,</p>
  <p>Claimed content was found in your video 'Delivery Driver Finds Secret Cheat Code For Dogs #shorts #funny'.</p>
  <table>
    <tr>
      <td>
        <span style="font-size:18px; font-weight:600;">Fortunate Son</span>
        <span style="font-size:18px; font-weight:400;">&nbsp;-&nbsp;Creedence Clearwater Revival</span>
      </td>
    </tr>
  </table>
  <p>Content owners: UMG</p>
  <p>Type: Audio</p>
  <p>Impact on video: No impact</p>
  <a href="https://studio.youtube.com/video/HcwZS5Sxv_Y/edit?d=vc">View all actions</a>
</div>
""",
}

FIXTURE_CLAIM_RELEASED_CAR = {
    "message_id": "msg_release_car_002",
    "subject": "Copyright claim was released: My Car Is Playing A Cooking Channel Instead Of Reversing #shorts #funny",
    "sender": "YouTube <no-reply@youtube.com>",
    "date": "Wed, 18 Nov 2026 02:10:00 -0800",
    "snippet": "Good news! The copyright claim on your video 'My Car Is Playing A Cooking Channel Instead Of Reversing #shorts #funny' has been released.",
    "body_text": """
Hi Betterverse,

Good news! The copyright claim on your video 'My Car Is Playing A Cooking Channel  
Instead Of Reversing #shorts #funny' has been released by CollabDRM.

Video ID: xwJJMYnUgSY
View video in Studio:
<https://studio.youtube.com/video/xwJJMYnUgSY/edit>
""",
    "body_html": """
<p>The copyright claim on your video 'My Car Is Playing A Cooking Channel Instead Of Reversing #shorts #funny' has been released.</p>
<a href="https://studio.youtube.com/video/xwJJMYnUgSY/edit">Studio</a>
""",
}

FIXTURE_VISUAL_CLAIM = {
    "message_id": "msg_visual_claim_003",
    "subject": "Video is blocked: Superhero action sequence #shorts #funny",
    "sender": "YouTube <no-reply@youtube.com>",
    "date": "Thu, 12 Nov 2026 10:00:00 -0800",
    "snippet": "Copyright-protected content has been detected in your Short 'Superhero action sequence #shorts #funny'. Your video has been blocked globally.",
    "body_text": """
Hi creator,

Copyright-protected content has been detected in your Short 'Superhero action  
sequence #shorts #funny'. As a result, your video has been blocked globally.  
This is not a copyright strike.

CONTENT USED
Pilot - Paramount Global

Content owners:  Paramount Global
Content type:  Video
Impact on video:  Blocked globally

<https://studio.youtube.com/video/asH5G5YV0I0/edit>
""",
    "body_html": "",
}

FIXTURE_STRIKE_EMAIL = {
    "message_id": "msg_strike_004",
    "subject": "Copyright strike: School Science Experiments Gone Wrong #shorts #fails",
    "sender": "YouTube <no-reply@youtube.com>",
    "date": "Mon, 16 Nov 2026 08:00:00 -0800",
    "snippet": "Your video has been removed due to a copyright takedown notice. You have received a copyright strike.",
    "body_text": """
Hi Betterverse,

Your video 'School Science Experiments Gone Wrong #shorts #fails' has been removed due to a copyright takedown notice from Science Media LLC.
As a result, your channel has received a copyright strike.

Video ID: asH5G5YV0I0
""",
    "body_html": "",
}

FIXTURE_OLDER_LEGACY_VIDEO = {
    "message_id": "msg_legacy_channel_005",
    "subject": "Video cannot be monetised: Video 399",
    "sender": "YouTube <no-reply@youtube.com>",
    "date": "Mon, 07 Apr 2025 14:34:31 -0700",
    "snippet": "Hi storyblocks, Copyright-protected content has been detected in your Short 'Video 399'.",
    "body_text": """
Hi storyblocks,
Copyright-protected content has been detected in your Short 'Video 399'.
CONTENT USED
Saturday Night Live

Content owners: NBCUniversal
Type: Visual
<https://studio.youtube.com/video/legacy_vid_999/edit>
""",
    "body_html": "",
}


import tempfile
import shutil

@pytest.fixture
def temp_db():
    """Create isolated test SQLite database initialized with pipeline schema."""
    temp_dir = tempfile.mkdtemp()
    db_file = Path(temp_dir) / "test_agent.db"
    db = AgentDatabase(db_file)

    future_dt = datetime.now(timezone.utc) + timedelta(days=2)
    target_date = future_dt.strftime("%Y-%m-%d")
    future_publish = future_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    future_publish_2 = (future_dt + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_publish_3 = (future_dt + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    future_slot_1 = f"{target_date}T12:00:00-05:00"
    future_slot_2 = f"{target_date}T14:00:00-05:00"
    future_slot_3 = f"{target_date}T17:00:00-05:00"
    created_at = f"{target_date}T00:00:00"

    with db._get_connection() as conn:
        # Seed test uploads
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, privacy, publish_at, rights_status, claim_status)
            VALUES 
              ('reddit_1xw42j0', ?, ?, 'scheduled', 'HcwZS5Sxv_Y', 'private', ?, 'unreviewed', 'none'),
              ('reddit_1xv7kmf', ?, ?, 'scheduled', 'xwJJMYnUgSY', 'private', ?, 'held', 'claimed'),
              ('reddit_1xryc9b', ?, ?, 'scheduled', 'asH5G5YV0I0', 'private', ?, 'cleared', 'none')
        """, (
            target_date, future_slot_1, future_publish,
            target_date, future_slot_2, future_publish_2,
            target_date, future_slot_3, future_publish_3,
        ))

        # Seed test schedule
        conn.execute("""
            INSERT INTO schedule (post_id, target_date, slot_hour, slot_time, video_path, title, uploader, status, rights_status, created_at)
            VALUES 
              ('reddit_1xw42j0', ?, 12, ?, '/videos/vid1.mp4', 'Delivery Driver Finds Secret Cheat Code For Dogs #shorts #funny', 'courier_guy', 'scheduled', 'unreviewed', ?),
              ('reddit_1xv7kmf', ?, 14, ?, '/videos/vid2.mp4', 'My Car Is Playing A Cooking Channel Instead Of Reversing #shorts #funny', 'car_dude', 'scheduled', 'held', ?),
              ('reddit_1xryc9b', ?, 17, ?, '/videos/vid3.mp4', 'School Science Experiments Gone Wrong #shorts #fails', 'science_kid', 'scheduled', 'cleared', ?)
        """, (
            target_date, future_slot_1, created_at,
            target_date, future_slot_2, created_at,
            target_date, future_slot_3, created_at,
        ))

        # Seed test reserve clip
        reserve_path = Path(temp_dir) / "reserve_video.mp4"
        reserve_path.write_text("dummy video")
        conn.execute("""
            INSERT INTO reserve_pool (post_id, video_path, title, score, status, rights_status, added_at)
            VALUES ('reddit_reserve_01', ?, 'Hilarious Dog Acrobatics #shorts #funny', 95.0, 'available', 'cleared', ?)
        """, (str(reserve_path), created_at))

    yield db
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


# ==============================================================================
# 1. PARSER TESTS
# ==============================================================================

def test_parse_claim_added_fortunate_son():
    f = FIXTURE_CLAIM_ADDED_FORTUNATE_SON
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    assert parsed.message_id == "msg_fortunate_son_001"
    assert parsed.event_type == "claim_added"
    assert parsed.video_id == "HcwZS5Sxv_Y"
    assert "Delivery Driver Finds Secret Cheat Code For Dogs" in parsed.video_title
    assert parsed.asset_title == "Fortunate Son"
    assert parsed.track_artist == "Creedence Clearwater Revival"
    assert parsed.claimant == "UMG"
    assert parsed.claim_type == "audio"
    assert parsed.affects_reach is False
    assert parsed.is_strike is False
    assert len(parsed.raw_snippet) <= 300


def test_parse_claim_released():
    f = FIXTURE_CLAIM_RELEASED_CAR
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    assert parsed.event_type == "claim_released"
    assert parsed.video_id == "xwJJMYnUgSY"
    assert parsed.is_strike is False


def test_parse_visual_claim_with_blocked_reach():
    f = FIXTURE_VISUAL_CLAIM
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    assert parsed.event_type == "claim_added"
    assert parsed.claim_type == "visual"
    assert parsed.affects_reach is True
    assert parsed.claimant == "Paramount Global"
    assert parsed.is_strike is False


def test_parse_strike_email():
    f = FIXTURE_STRIKE_EMAIL
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    assert parsed.is_strike is True
    assert parsed.event_type == "strike"


def test_parse_studio_paste():
    paste = """
    Fortunate Son - Creedence Clearwater Revival
    Content used: Audio
    Impact on video: No impact
    Claimant: UMG
    """
    res = parse_studio_paste(paste)
    assert res["asset_title"] == "Fortunate Son"
    assert res["track_artist"] == "Creedence Clearwater Revival"
    assert res["claimant"] == "UMG"
    assert res["claim_type"] == "audio"
    assert res["affects_reach"] is False


# ==============================================================================
# 2. MATCHER & DEDUPLICATION TESTS
# ==============================================================================

def test_match_by_video_id(temp_db):
    matcher = ClaimsMatcher(temp_db)
    parsed = ParsedClaimEmail(
        message_id="m1",
        event_type="claim_added",
        video_id="HcwZS5Sxv_Y",
    )
    up = matcher.match_email_to_upload(parsed)
    assert up is not None
    assert up["post_id"] == "reddit_1xw42j0"


def test_match_by_title_ignoring_hashtags(temp_db):
    matcher = ClaimsMatcher(temp_db)
    parsed = ParsedClaimEmail(
        message_id="m2",
        event_type="claim_added",
        video_title="delivery driver finds secret cheat code for dogs",  # no hashtags, lowercase
    )
    up = matcher.match_email_to_upload(parsed)
    assert up is not None
    assert up["post_id"] == "reddit_1xw42j0"


def test_older_channel_video_safeguard(temp_db):
    """Videos not in uploads table must NEVER match or trigger pipeline actions."""
    matcher = ClaimsMatcher(temp_db)
    f = FIXTURE_OLDER_LEGACY_VIDEO
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        snippet=f["snippet"],
    )

    up = matcher.match_email_to_upload(parsed)
    assert up is None  # Safeguard: not matched


def test_claim_deduplication(temp_db):
    """Verify deduplication when duplicate emails arrive for the same claim."""
    matcher = ClaimsMatcher(temp_db)
    parsed = ParsedClaimEmail(
        message_id="m_dup_1",
        event_type="claim_added",
        video_id="new_vid_999",
        asset_title="Brand New Song",
        claimant="Sony Music",
        claim_type="audio",
    )
    upload = {"post_id": "reddit_new_clip", "video_id": "new_vid_999"}

    claim_id_1, act_1 = matcher.upsert_claim(upload, parsed)
    assert act_1 == "created"

    # Second email for same claim
    parsed2 = ParsedClaimEmail(
        message_id="m_dup_2",
        event_type="claim_added",
        video_id="new_vid_999",
        asset_title="Brand New Song",
        claimant="Sony Music",
        claim_type="audio",
    )
    claim_id_2, act_2 = matcher.upsert_claim(upload, parsed2)
    assert claim_id_2 == claim_id_1  # Deduplicated to same row
    assert act_2 == "updated"


def test_email_claim_upgrades_manual_placeholder(temp_db):
    """
    The manual claim (claimant 'unknown') and the email claim (UMG)
    for the same video must NOT result in two rows.
    The email must upgrade the manual placeholder in-place.
    """
    matcher = ClaimsMatcher(temp_db)
    f = FIXTURE_CLAIM_ADDED_FORTUNATE_SON
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None
    assert upload["post_id"] == "reddit_1xw42j0"

    # Verify initial placeholder exists
    with temp_db._get_connection() as conn:
        before = conn.execute("SELECT * FROM claims WHERE post_id = 'reddit_1xw42j0'").fetchall()
        assert len(before) == 1
        placeholder_id = before[0]["id"]
        assert before[0]["claimant"].lower() == "unknown"
        assert before[0]["source"] == "manual"

    # Upsert email claim
    claim_id, action = matcher.upsert_claim(upload, parsed, source="email")
    assert claim_id == placeholder_id
    assert action == "updated"

    # Verify DB has only 1 row and fields are upgraded
    with temp_db._get_connection() as conn:
        after = conn.execute("SELECT * FROM claims WHERE post_id = 'reddit_1xw42j0'").fetchall()
        assert len(after) == 1
        assert after[0]["id"] == placeholder_id
        assert after[0]["claimant"] == "UMG"
        assert after[0]["source"] == "email"
        assert after[0]["message_id"] == "msg_fortunate_son_001"
        assert after[0]["track_artist"] == "Creedence Clearwater Revival"
        assert after[0]["state"] == "active"


def test_manual_claim_does_not_merge_into_different_track_email_claim(temp_db):
    """
    A manual claim for one track (e.g., 'Fortunate Son') must NOT merge into
    an email claim for a different track (e.g., 'Bad Moon Rising') on the same video,
    even though both are audio claims.
    """
    matcher = ClaimsMatcher(temp_db)
    # reddit_1xw42j0 already has a manual claim for 'Fortunate Son'
    upload = {"post_id": "reddit_1xw42j0", "video_id": "HcwZS5Sxv_Y"}

    with temp_db._get_connection() as conn:
        before = conn.execute("SELECT * FROM claims WHERE post_id = 'reddit_1xw42j0'").fetchall()
        assert len(before) == 1
        placeholder_id = before[0]["id"]
        assert before[0]["asset_title"] == "Fortunate Son"

    # Incoming email for a different audio track on the same video
    parsed_diff = ParsedClaimEmail(
        message_id="msg_diff_track_001",
        event_type="claim_added",
        video_id="HcwZS5Sxv_Y",
        video_title="Delivery Driver Finds Secret Cheat Code For Dogs",
        asset_title="Bad Moon Rising",
        track_artist="CCR",
        claimant="Fantasy Records",
        claim_type="audio",
    )

    claim_id, action = matcher.upsert_claim(upload, parsed_diff, source="email")
    assert action == "created"
    assert claim_id != placeholder_id

    # Verify both claims now exist for this video and neither was overwritten or deleted
    with temp_db._get_connection() as conn:
        all_claims = conn.execute("SELECT * FROM claims WHERE post_id = 'reddit_1xw42j0' ORDER BY id ASC").fetchall()
        assert len(all_claims) == 2
        # Original manual placeholder preserved
        assert all_claims[0]["id"] == placeholder_id
        assert all_claims[0]["asset_title"] == "Fortunate Son"
        assert all_claims[0]["claimant"].lower() == "unknown"
        assert all_claims[0]["source"] == "manual"
        # New email claim inserted
        assert all_claims[1]["id"] == claim_id
        assert all_claims[1]["asset_title"] == "Bad Moon Rising"
        assert all_claims[1]["claimant"] == "Fantasy Records"
        assert all_claims[1]["source"] == "email"


def test_track_titles_match_helper():
    from claims_sync.matcher import track_titles_match
    assert track_titles_match("Fortunate Son", "Fortunate Son") is True
    assert track_titles_match("Fortunate Son", "fortunate son") is True
    assert track_titles_match("Fortunate Son", "Fortunate Son (2020 Remaster)") is True
    assert track_titles_match("Fortunate Son", "Fortunate Son - Creedence Clearwater Revival") is True
    assert track_titles_match("Fortunate Son", "Bad Moon Rising") is False
    assert track_titles_match("Fortunate Son", "Unknown Asset") is False
    assert track_titles_match("Unknown Asset", "Unknown Asset") is False
    assert track_titles_match(None, "Fortunate Son") is False


# ==============================================================================
# 3. ACCEPTANCE CRITERIA
# ==============================================================================

@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_acceptance_1_dry_run_simulation(mock_notify, mock_config, temp_db):
    """
    Acceptance 1: with dry-run on, the Fortunate Son email for reddit_1xw42j0 is detected,
    upgrades the manual placeholder to an active audio claim, reversible hold is applied to the scheduled clip,
    and planned action 'delete and replace from reserve' is logged without calling YouTube delete.
    """
    mock_config.return_value = {
        "dry_run": True,
        "max_auto_actions_per_day": 6,
        "auto_action_scheduled": "delete",
    }

    matcher = ClaimsMatcher(temp_db)
    f = FIXTURE_CLAIM_ADDED_FORTUNATE_SON
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None
    assert upload["post_id"] == "reddit_1xw42j0"

    claim_id, action = matcher.upsert_claim(upload, parsed, source="email")
    assert action in ("created", "updated")

    # Verify only 1 claim row exists for reddit_1xw42j0 (no duplicate manual + email rows)
    with temp_db._get_connection() as conn:
        c_rows = conn.execute("SELECT * FROM claims WHERE post_id = 'reddit_1xw42j0'").fetchall()
        assert len(c_rows) == 1
        assert c_rows[0]["claimant"] == "UMG"
        assert c_rows[0]["source"] == "email"

    # Mock youtube service: MUST NOT receive any delete call!
    mock_yt = MagicMock()

    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(
            upload=upload,
            claim_id=claim_id,
            parsed=parsed,
            youtube_service=mock_yt,
        )

    assert res["action"] == "simulate_delete_and_replace"
    assert res["dry_run"] is True
    assert res["held"] is True
    # Verify no YouTube delete was called
    mock_yt.videos().delete.assert_not_called()

    # Verify reversible hold in DB
    with temp_db._get_connection() as conn:
        row_up = conn.execute("SELECT rights_status, publish_at FROM uploads WHERE post_id = 'reddit_1xw42j0'").fetchone()
        assert row_up["rights_status"] == "held"
        assert row_up["publish_at"] is None

        # Verify audit row
        audit = conn.execute("SELECT * FROM claim_actions WHERE post_id = 'reddit_1xw42j0'").fetchone()
        assert audit is not None
        assert audit["dry_run"] == 1
        assert audit["action_type"] == "simulate_delete_and_replace"


@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_acceptance_2_live_delete_and_replace(mock_notify, mock_config, temp_db):
    """
    Acceptance 2: with a mocked YouTube client and dry-run off, the same email deletes
    that video, fills the slot from the reserve, and writes an audit row.
    """
    mock_config.return_value = {
        "dry_run": False,
        "max_auto_actions_per_day": 6,
        "auto_action_scheduled": "delete",
    }

    matcher = ClaimsMatcher(temp_db)
    f = FIXTURE_CLAIM_ADDED_FORTUNATE_SON
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        body_html=f["body_html"],
        snippet=f["snippet"],
    )

    upload = matcher.match_email_to_upload(parsed)
    claim_id, _ = matcher.upsert_claim(upload, parsed, source="email")

    mock_yt = MagicMock()

    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(
            upload=upload,
            claim_id=claim_id,
            parsed=parsed,
            youtube_service=mock_yt,
        )

    assert res["success"] is True
    assert res["dry_run"] is False
    assert res["action"] == "delete_and_replace"

    # YouTube delete was called on claimed video ID
    mock_yt.videos().delete.assert_called_once_with(id="HcwZS5Sxv_Y")

    # Slot backfilled with reserve clip in SQLite
    with temp_db._get_connection() as conn:
        sched = conn.execute("SELECT post_id, rights_status FROM schedule WHERE slot_hour = 12").fetchone()
        assert sched["post_id"] == "reddit_reserve_01"
        assert sched["rights_status"] == "cleared"

        # Audit row written
        audit = conn.execute("SELECT * FROM claim_actions WHERE action_type = 'delete_and_replace'").fetchone()
        assert audit is not None
        assert audit["dry_run"] == 0
        assert audit["result"] == "success"


def test_acceptance_3_claim_released_car_clip(temp_db):
    """
    Acceptance 3: the car clip no longer shows 'Claimed' after a 'claim released' fixture.
    """
    matcher = ClaimsMatcher(temp_db)
    f = FIXTURE_CLAIM_RELEASED_CAR
    parsed = parse_claim_email(
        message_id=f["message_id"],
        subject=f["subject"],
        body_text=f["body_text"],
        snippet=f["snippet"],
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None
    assert upload["post_id"] == "reddit_1xv7kmf"

    claim_id, action = matcher.upsert_claim(upload, parsed, source="email")
    assert action == "released"

    with temp_db._get_connection() as conn:
        c_row = conn.execute("SELECT state, released_at FROM claims WHERE id = ?", (claim_id,)).fetchone()
        assert c_row["state"] == "released"
        assert c_row["released_at"] is not None

        up_row = conn.execute("SELECT claim_status FROM uploads WHERE post_id = 'reddit_1xv7kmf'").fetchone()
        assert up_row["claim_status"] == "released"


# ==============================================================================
# 4. STRIKE & SAFETY SAFEGUARDS
# ==============================================================================

@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_strike_never_auto_deletes(mock_notify, mock_config, temp_db):
    """A strike email raises an urgent alert and NEVER auto-deletes."""
    mock_config.return_value = {"dry_run": False, "max_auto_actions_per_day": 6}

    f = FIXTURE_STRIKE_EMAIL
    parsed = parse_claim_email(f["message_id"], f["subject"], f["body_text"], snippet=f["snippet"])
    assert parsed.is_strike is True

    matcher = ClaimsMatcher(temp_db)
    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None

    mock_yt = MagicMock()
    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(upload, claim_id=99, parsed=parsed, youtube_service=mock_yt)

    assert res["is_strike"] is True
    assert res["action"] == "alert_strike"
    mock_yt.videos().delete.assert_not_called()
    mock_notify.assert_called()


@patch("claims_sync.sync.notify_rights_alert")
def test_missing_video_probe_circuit_breaker_halts_when_more_than_half_missing(mock_notify, temp_db):
    """
    If > 50% of clips appear missing (e.g. YouTube returned empty items or wrong channel),
    the probe must halt immediately, alert the operator, and NOT mark clips deleted.
    """
    mock_yt = MagicMock()
    # All 3 videos missing (100% > 50%)
    mock_yt.videos().list().execute.return_value = {"items": []}

    with patch("claims_sync.sync.DB_PATH", temp_db.db_path):
        missing = probe_missing_videos_on_youtube(youtube_service=mock_yt)

    # Circuit breaker halted probe
    assert missing == []
    mock_notify.assert_called_once()
    assert "CRITICAL: Missing Video Probe Safety Halt" in mock_notify.call_args[0][0]

    # Verify no uploads were marked deleted
    with temp_db._get_connection() as conn:
        rows = conn.execute("SELECT status FROM uploads WHERE status = 'deleted'").fetchall()
        assert len(rows) == 0


def test_missing_video_probe_under_threshold_marks_deleted_and_frees_slot(temp_db):
    """
    When <= 50% of clips are missing (e.g. 1 out of 3), the missing clip is safely marked deleted
    and its slot marked empty.
    """
    mock_yt = MagicMock()
    # 2 found, 1 missing (HcwZS5Sxv_Y) -> 1/3 (33% <= 50%)
    mock_yt.videos().list().execute.return_value = {
        "items": [
            {"id": "xwJJMYnUgSY"},
            {"id": "asH5G5YV0I0"},
        ]
    }

    with patch("claims_sync.sync.DB_PATH", temp_db.db_path):
        missing = probe_missing_videos_on_youtube(youtube_service=mock_yt)

    assert len(missing) == 1
    assert missing[0]["video_id"] == "HcwZS5Sxv_Y"

    with temp_db._get_connection() as conn:
        del_up = conn.execute("SELECT status FROM uploads WHERE video_id = 'HcwZS5Sxv_Y'").fetchone()
        assert del_up["status"] == "deleted"

        del_sched = conn.execute("SELECT status FROM schedule WHERE post_id = 'reddit_1xw42j0'").fetchone()
        assert del_sched["status"] == "empty"


@patch("claims_sync.sync.subprocess.Popen")
def test_missing_video_probe_only_triggers_fills_for_future_dates(mock_popen, temp_db):
    """
    Probe must ONLY trigger curate.py --fill-slots for future dates.
    Past test dates must be strictly skipped.
    """
    # Seed 5 videos: 3 present, 1 past missing, 1 future missing -> 2/5 (40% <= 50%)
    now = datetime.now(timezone.utc)
    past_d = (now - timedelta(days=4)).strftime("%Y-%m-%d")
    fut_d = (now + timedelta(days=6)).strftime("%Y-%m-%d")

    past_slot = f"{past_d}T12:00:00-05:00"
    past_pub = f"{past_d}T17:00:00Z"
    fut_slot = f"{fut_d}T14:00:00-05:00"
    fut_pub = f"{fut_d}T19:00:00Z"

    with temp_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, privacy, publish_at)
            VALUES 
              ('past_clip', ?, ?, 'scheduled', 'past_vid_999', 'public', ?),
              ('future_clip', ?, ?, 'scheduled', 'fut_vid_888', 'private', ?)
        """, (past_d, past_slot, past_pub, fut_d, fut_slot, fut_pub))

    mock_yt = MagicMock()
    # Return 3 existing videos, leaving past_vid_999 and fut_vid_888 missing
    mock_yt.videos().list().execute.return_value = {
        "items": [
            {"id": "HcwZS5Sxv_Y"},
            {"id": "xwJJMYnUgSY"},
            {"id": "asH5G5YV0I0"},
        ]
    }

    with patch("claims_sync.sync.DB_PATH", temp_db.db_path):
        missing = probe_missing_videos_on_youtube(youtube_service=mock_yt)

    assert len(missing) == 2

    # Check that subprocess.Popen was called for future_clip (fut_d)
    # and NEVER for past_clip (past_d)
    called_commands = [call_args[0][0] for call_args in mock_popen.call_args_list]
    assert any(fut_d in cmd for cmd in called_commands)
    assert not any(past_d in cmd for cmd in called_commands)


# ==============================================================================
# 5. SCHEDULED VS PUBLIC TIMING TESTS
# ==============================================================================

from claims_sync.actions import determine_clip_scheduled_state
from datetime import datetime, timezone, timedelta

def test_determine_clip_scheduled_state_timing():
    """
    Test timing classification:
    - Future publish_at is scheduled.
    - Past publish_at is public even if status == 'scheduled'.
    - Live YouTube status 'public' is public even if status == 'scheduled'.
    """
    now_utc = datetime.now(timezone.utc)
    future_pub = (now_utc + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past_pub = (now_utc - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Future publish_at -> scheduled
    up_future = {
        "post_id": "test_future",
        "video_id": "vid_fut",
        "status": "scheduled",
        "privacy": "private",
        "publish_at": future_pub,
    }
    is_sched, reason = determine_clip_scheduled_state(up_future)
    assert is_sched is True
    assert "in the future" in reason

    # 2. Past publish_at -> public (even though status == 'scheduled'!)
    up_past = {
        "post_id": "test_past",
        "video_id": "vid_past",
        "status": "scheduled",
        "privacy": "private",
        "publish_at": past_pub,
    }
    is_sched, reason = determine_clip_scheduled_state(up_past)
    assert is_sched is False
    assert "has passed" in reason

    # 3. Live YouTube status 'public' -> public
    mock_yt_pub = MagicMock()
    mock_yt_pub.videos().list().execute.return_value = {
        "items": [{"id": "vid_live", "status": {"privacyStatus": "public"}}]
    }
    up_live_pub = {
        "post_id": "test_live_pub",
        "video_id": "vid_live",
        "status": "scheduled",
        "privacy": "private",
        "publish_at": future_pub,  # DB says future, but live YouTube says already public
    }
    is_sched, reason = determine_clip_scheduled_state(up_live_pub, youtube_service=mock_yt_pub)
    assert is_sched is False
    assert "Live YouTube status is 'public'" in reason

    # 4. Live YouTube status 'private' with future publishAt -> scheduled
    mock_yt_sched = MagicMock()
    mock_yt_sched.videos().list().execute.return_value = {
        "items": [{"id": "vid_sched", "status": {"privacyStatus": "private", "publishAt": future_pub}}]
    }
    up_live_sched = {
        "post_id": "test_live_sched",
        "video_id": "vid_sched",
        "status": "scheduled",
    }
    is_sched, reason = determine_clip_scheduled_state(up_live_sched, youtube_service=mock_yt_sched)
    assert is_sched is True
    assert "Live YouTube publishAt is in the future" in reason


@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_already_public_clip_claim_follows_public_policy_dry_run(mock_notify, mock_config, temp_db):
    """
    A claim on an already-public clip (past publish_at) must follow public policy,
    NOT hold or replace-from-reserve. In dry-run, it logs simulate_delete_public.
    """
    mock_config.return_value = {
        "dry_run": True,
        "max_auto_actions_per_day": 6,
        "auto_action_public": "delete",
        "auto_action_scheduled": "delete",
    }

    now_utc = datetime.now(timezone.utc)
    past_pub = (now_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with temp_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, privacy, publish_at, rights_status, claim_status)
            VALUES ('reddit_public_clip', '2026-11-17', '2026-11-17T12:00:00-05:00', 'scheduled', 'pub_vid_123', 'public', ?, 'unreviewed', 'none')
        """, (past_pub,))

    matcher = ClaimsMatcher(temp_db)
    parsed = parse_claim_email(
        message_id="msg_pub_001",
        subject="Video cannot be monetised: Public Video Title #shorts #funny",
        body_text="Hi creator, Content used: Fortunate Son by CCR. Content owners: UMG. Type: Audio.\n<https://studio.youtube.com/video/pub_vid_123/edit>",
        snippet="Copyright claim on Public Video Title",
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None
    assert upload["post_id"] == "reddit_public_clip"

    claim_id, _ = matcher.upsert_claim(upload, parsed, source="email")

    mock_yt = MagicMock()
    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(
            upload=upload,
            claim_id=claim_id,
            parsed=parsed,
            youtube_service=mock_yt,
        )

    # Must follow public policy (simulate_delete_public), NOT simulate_delete_and_replace!
    assert res["action"] == "simulate_delete_public"
    assert res["dry_run"] is True
    # YouTube delete must not be called in dry-run
    mock_yt.videos().delete.assert_not_called()

    # Reserve pool must NOT have been touched
    with temp_db._get_connection() as conn:
        reserve_row = conn.execute("SELECT status FROM reserve_pool WHERE post_id = 'reddit_reserve_01'").fetchone()
        assert reserve_row["status"] == "available"

        audit = conn.execute("SELECT * FROM claim_actions WHERE post_id = 'reddit_public_clip'").fetchone()
        assert audit is not None
        assert audit["action_type"] == "simulate_delete_public"
        assert audit["dry_run"] == 1


@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_already_public_clip_claim_follows_public_policy_live_delete(mock_notify, mock_config, temp_db):
    """
    With dry_run off, a claim on a public clip calls videos().delete on YouTube
    and marks status = 'deleted', but does NOT replace from reserve.
    """
    mock_config.return_value = {
        "dry_run": False,
        "max_auto_actions_per_day": 6,
        "auto_action_public": "delete",
        "auto_action_scheduled": "delete",
    }

    now_utc = datetime.now(timezone.utc)
    past_pub = (now_utc - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with temp_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, privacy, publish_at, rights_status, claim_status)
            VALUES ('reddit_public_live', '2026-11-16', '2026-11-16T14:00:00-05:00', 'scheduled', 'pub_vid_456', 'public', ?, 'unreviewed', 'none')
        """, (past_pub,))

    matcher = ClaimsMatcher(temp_db)
    parsed = parse_claim_email(
        message_id="msg_pub_002",
        subject="Video cannot be monetised: Live Public Video Title #shorts #funny",
        body_text="Hi creator, Content used: Fortunate Son by CCR. Content owners: UMG. Type: Audio.\n<https://studio.youtube.com/video/pub_vid_456/edit>",
        snippet="Copyright claim on Live Public Video Title",
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None
    assert upload["post_id"] == "reddit_public_live"

    claim_id, _ = matcher.upsert_claim(upload, parsed, source="email")

    mock_yt = MagicMock()
    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(
            upload=upload,
            claim_id=claim_id,
            parsed=parsed,
            youtube_service=mock_yt,
        )

    # Must execute delete_public
    assert res["action"] == "delete_public"
    assert res["dry_run"] is False

    # Live YouTube delete called for pub_vid_456
    mock_yt.videos().delete.assert_called_once_with(id="pub_vid_456")

    with temp_db._get_connection() as conn:
        up_row = conn.execute("SELECT status FROM uploads WHERE post_id = 'reddit_public_live'").fetchone()
        assert up_row["status"] == "deleted"

        # Reserve pool was NOT used for public clip replacement
        res_pool = conn.execute("SELECT status FROM reserve_pool WHERE post_id = 'reddit_reserve_01'").fetchone()
        assert res_pool["status"] == "available"

        audit = conn.execute("SELECT * FROM claim_actions WHERE post_id = 'reddit_public_live'").fetchone()
        assert audit is not None
        assert audit["action_type"] == "delete_public"
        assert audit["dry_run"] == 0


@patch("claims_sync.actions.load_claims_config")
@patch("claims_sync.actions.notify_rights_alert")
def test_already_public_clip_delete_failure_records_failed_and_does_not_mark_deleted(mock_notify, mock_config, temp_db):
    """
    If videos.delete fails for a public clip, the code must NOT mark it deleted in SQLite,
    must log result='failed' in claim_actions, and must return success=False.
    """
    mock_config.return_value = {
        "dry_run": False,
        "max_auto_actions_per_day": 6,
        "auto_action_public": "delete",
    }

    now_utc = datetime.now(timezone.utc)
    past_pub = (now_utc - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with temp_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, slot_time, status, video_id, privacy, publish_at, rights_status, claim_status)
            VALUES ('reddit_public_fail', '2026-11-16', '2026-11-16T14:00:00-05:00', 'scheduled', 'fail_vid_45', 'public', ?, 'unreviewed', 'none')
        """, (past_pub,))

    matcher = ClaimsMatcher(temp_db)
    parsed = parse_claim_email(
        message_id="msg_pub_fail_003",
        subject="Video cannot be monetised: Fail Public Video Title #shorts #funny",
        body_text="Hi creator, Content used: Fortunate Son by CCR. Content owners: UMG. Type: Audio.\n<https://studio.youtube.com/video/fail_vid_45/edit>",
        snippet="Copyright claim on Fail Public Video Title",
    )

    upload = matcher.match_email_to_upload(parsed)
    assert upload is not None

    claim_id, _ = matcher.upsert_claim(upload, parsed, source="email")

    # Mock youtube service where delete raises an exception (e.g. 403 Forbidden)
    mock_yt = MagicMock()
    mock_yt.videos().delete().execute.side_effect = Exception("403 Forbidden: Insufficient Permission")

    with patch("claims_sync.actions.DB_PATH", temp_db.db_path):
        res = execute_claim_action(
            upload=upload,
            claim_id=claim_id,
            parsed=parsed,
            youtube_service=mock_yt,
        )

    # Must return success = False
    assert res["success"] is False
    assert res["action"] == "delete_public"
    assert "403 Forbidden" in res["error"]

    # Verify upload row was NOT marked deleted in SQLite!
    with temp_db._get_connection() as conn:
        up_row = conn.execute("SELECT status FROM uploads WHERE post_id = 'reddit_public_fail'").fetchone()
        assert up_row["status"] != "deleted"
        assert up_row["status"] == "scheduled"

        # Verify audit log recorded 'failed'
        audit = conn.execute("SELECT * FROM claim_actions WHERE post_id = 'reddit_public_fail'").fetchone()
        assert audit is not None
        assert audit["action_type"] == "delete_public"
        assert audit["result"] == "failed"
        assert audit["dry_run"] == 0
        details = json.loads(audit["details_json"])
        assert "403 Forbidden" in details["error"]

    # Verify alert was dispatched
    mock_notify.assert_called()

