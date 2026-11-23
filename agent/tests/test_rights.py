import os
import json
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from core.rights.matcher import BlocklistMatcher, normalize_text, levenshtein_distance
from core.rights.ocr import VideoOCRChecker
from core.rights.audio_check import AudioRightsChecker
from core.rights.visual_match import VisualMatchChecker
from core.rights.perceptual import HistoryClaimsChecker
from core.rights.engine import RightsCheckEngine
from core.rights.youtube_gate import YouTubeRightsGate
from core.db import AgentDatabase


# ==============================================================================
# 1. LAYER 1: MATCHER & OCR TESTS
# ==============================================================================

def test_normalization_and_distance():
    assert normalize_text("Viral-Hog!!!") == "viralhog"
    assert normalize_text("Storyful, Inc.") == "storyfulinc"
    assert normalize_text("  Jukin   Media  ") == "jukinmedia"

    assert levenshtein_distance("viralhog", "viralhog") == 0
    assert levenshtein_distance("viralhog", "viralhogg") == 1
    assert levenshtein_distance("storyful", "storyfull") == 1
    assert levenshtein_distance("caters", "katerz") == 2
    assert levenshtein_distance("caters", "completelydifferent") > 3


def test_matcher_agency_matching():
    matcher = BlocklistMatcher()

    # Exact and aliased names
    assert matcher.check_agency_match("Video licensed by ViralHog") is not None
    assert matcher.check_agency_match("viral hog video") is not None
    assert matcher.check_agency_match("Courtesy of Jukin Media") is not None
    assert matcher.check_agency_match("Storyful Rights Reserved") is not None
    assert matcher.check_agency_match("CollabDRM Content") is not None
    assert matcher.check_agency_match("Collab Media video") is not None
    assert matcher.check_agency_match("Barcroft Media video") is not None

    # Whole-word match for short names under 8 letters
    assert matcher.check_agency_match("Licensed by Jukin") is not None
    assert matcher.check_agency_match("Video licensed by Caters News") is not None
    assert matcher.check_agency_match("Video licensed by Caters Clips") is not None

    # Typo tolerance allowed ONLY for longer names (>= 8 letters)
    assert matcher.check_agency_match("Licensed by ViralHogg") is not None
    assert matcher.check_agency_match("Storyfull News") is not None

    # Prevented false blocks on short names / generic aliases
    assert matcher.check_agency_match("Pizza caters to everyone") is None  # 'Caters' dropped as generic word
    assert matcher.check_agency_match("Video licensed by Caters") is None  # Bare 'Caters' is not blocked
    assert matcher.check_agency_match("Dad joking around") is None  # Jukin (< 8) does not typo match 'joking'
    assert matcher.check_agency_match("Truly the best parking job") is None  # 'Truly' dropped as generic alias
    assert matcher.check_agency_match("Waters rising") is None  # 'waters' is not blocked
    assert matcher.check_agency_match("Capers") is None  # 'capers' is not blocked
    assert matcher.check_agency_match("Collab with my dog") is None  # 'Collab' dropped as generic alias

    # Negative check (normal titles)
    assert matcher.check_agency_match("Dog catches a frisbee in the park") is None
    assert matcher.check_agency_match("Hilarious fail at skating rink") is None


def test_matcher_domain_matching():
    matcher = BlocklistMatcher()
    assert matcher.check_domain_match("https://www.viralhog.com/v/12345") == "ViralHog"
    assert matcher.check_domain_match("http://storyful.com/news") == "Storyful"
    assert matcher.check_domain_match("https://i.imgur.com/gallery/abc") is None
    assert matcher.check_domain_match("https://v.redd.it/xyz123") is None


def test_matcher_learned_blocklist():
    matcher = BlocklistMatcher()
    active_blocklist = [
        {"id": 1, "value": "scammer_channel", "type": "uploader"},
        {"id": 2, "value": "stolenclips", "type": "subreddit"},
        {"id": 3, "value": "bannedagency", "type": "text"},
    ]

    # Uploader match
    hit = matcher.check_learned_blocklist(active_blocklist, uploader="scammer_channel")
    assert hit is not None
    assert hit["value"] == "scammer_channel"

    # Subreddit match
    hit = matcher.check_learned_blocklist(active_blocklist, subreddit="r/stolenclips")
    assert hit is not None
    assert hit["value"] == "stolenclips"

    # Title keyword match
    hit = matcher.check_learned_blocklist(active_blocklist, title="A video by bannedagency")
    assert hit is not None
    assert hit["value"] == "bannedagency"

    # Non-matching
    assert matcher.check_learned_blocklist(active_blocklist, title="Clean video", uploader="good_guy", subreddit="r/funny") is None


def test_ocr_corner_box_classification():
    ocr_checker = VideoOCRChecker()
    frame_w, frame_h = 1080, 1920

    # Top-right corner box
    tr_box = [[950, 40], [1050, 40], [1050, 100], [950, 100]]
    assert ocr_checker.is_in_corner(tr_box, frame_w, frame_h) is True

    # Top-left corner box
    tl_box = [[20, 20], [150, 20], [150, 80], [20, 80]]
    assert ocr_checker.is_in_corner(tl_box, frame_w, frame_h) is True

    # Bottom-right corner box
    br_box = [[920, 1800], [1060, 1800], [1060, 1900], [920, 1900]]
    assert ocr_checker.is_in_corner(br_box, frame_w, frame_h) is True

    # Center box (e.g. subtitle or meme text in middle)
    center_box = [[400, 900], [680, 900], [680, 980], [400, 980]]
    assert ocr_checker.is_in_corner(center_box, frame_w, frame_h) is False


def test_ocr_checker_decision_corner_vs_multiframe():
    ocr_checker = VideoOCRChecker()

    # Case A: Social watermark in a corner -> hard block
    box_corner = [[950, 40], [1050, 40], [1050, 100], [950, 100]]
    fake_frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    ocr_checker.sample_frames = MagicMock(return_value=[fake_frame])
    ocr_checker.ocr = MagicMock(return_value=([[box_corner, "tiktok", 0.95]], None))

    res_corner = ocr_checker.inspect_frames("dummy.mp4")
    assert res_corner["hard_block_social"] is True
    assert res_corner["review_social"] is False

    # Case B: Social watermark in center in only 1 frame -> review, not hard block
    box_center = [[400, 900], [680, 900], [680, 980], [400, 980]]
    ocr_checker.sample_frames = MagicMock(return_value=[fake_frame])
    ocr_checker.ocr = MagicMock(return_value=([[box_center, "tiktok", 0.95]], None))

    res_center = ocr_checker.inspect_frames("dummy.mp4")
    assert res_center["hard_block_social"] is False
    assert res_center["review_social"] is True


# ==============================================================================
# 2. LAYER 2: AUDIO CHECK & PROVIDER OPTIMIZATION
# ==============================================================================

def test_audio_checker_caps_and_samples():
    mock_provider = MagicMock()
    mock_provider.recognize.return_value = {
        "status": "success",
        "recognized": True,
        "title": "Eye of the Tiger",
        "artist": "Survivor",
        "label": "Sony Music Entertainment",
    }

    audio_checker = AudioRightsChecker(provider=mock_provider, daily_cap=40, multi_sample_threshold=0.60)

    # 1. Enforce daily cap
    cap_res = audio_checker.check_audio("dummy.mp4", current_daily_calls=40)
    assert cap_res["cap_reached"] is True
    assert cap_res["provider_calls_made"] == 0

    # 2. Sample window extraction logic
    fake_audio = np.zeros(16000 * 30, dtype=np.float32)  # 30 seconds
    samples = audio_checker.get_audio_samples(fake_audio, duration=30.0)
    assert len(samples) >= 1
    assert len(samples[0][1]) == 12 * 16000  # 12 seconds exactly


def test_fingerprint_provider_error_handling(monkeypatch):
    """
    Test that provider error responses (e.g. Shazam rate limit) are treated as provider errors:
    status = 'error', provider_errors incremented, reasons include 'provider_error_review',
    decision escalates to 'review', and errors counter is recorded.
    """
    mock_provider = MagicMock()
    mock_provider.recognize.return_value = {
        "status": "error",
        "recognized": False,
        "error": "Shazam request failed: HTTP 429 Too Many Requests",
        "error_code": None,
    }

    mock_detector = MagicMock()
    mock_detector.detect_music.return_value = {
        "music_seconds": 12.0,
        "music_fraction": 0.8,
        "has_music": True,
        "model_active": True,
    }

    audio_checker = AudioRightsChecker(
        local_detector=mock_detector,
        provider=mock_provider,
        daily_cap=40,
    )

    # Monkeypatch extract_audio_16k_mono to return synthetic audio
    fake_audio = np.zeros(16000 * 15, dtype=np.float32)
    import core.rights.audio_check as ac_mod
    monkeypatch.setattr(ac_mod, "extract_audio_16k_mono", lambda p: (fake_audio, 15.0))

    audio_res = audio_checker.check_audio("test_video.mp4", current_daily_calls=0)

    # Must treat status as error, count provider errors, and not call further samples
    assert audio_res["status"] == "error"
    assert audio_res["provider_calls_made"] == 1
    assert audio_res["provider_errors"] == 1
    assert audio_res["provider_error_code"] is None
    assert "429" in audio_res["error"]

    # Now verify RightsCheckEngine handles this audio_res
    mock_db = MagicMock()
    mock_db.get_provider_usage.return_value = {"calls": 0, "errors": 0}
    mock_db.count_real_published.return_value = 0

    engine = RightsCheckEngine(
        db=mock_db,
        audio_checker=audio_checker,
    )

    import core.rights.engine as eng_mod
    monkeypatch.setattr(
        eng_mod,
        "compute_video_phashes",
        lambda *args, **kwargs: ["0" * 16 + ":" + "0" * 16],
    )

    eval_res = engine.evaluate_clip(
        post_id="post_test_fingerprint_err",
        video_path="test_video.mp4",
        title="Test Clip",
    )

    assert "provider_error_review" in eval_res["reasons"]
    assert eval_res["decision"] == "review"
    # Ensure provider usage was incremented with errors
    mock_db.increment_provider_usage.assert_called_with("shazam", count=1, errors=1)


# ==============================================================================
# 3. LAYER 3: VISUAL MATCH CHECK
# ==============================================================================

def test_visual_checker_local_only():
    visual_checker = VisualMatchChecker(local_only=True, daily_cap=25)
    res = visual_checker.check_video("dummy.mp4")

    # In local-only mode, provider calls are skipped without errors
    assert res["local_only"] is True
    assert res["hard_block"] is False
    assert res["calls_made"] == 0


def test_visual_checker_daily_cap():
    visual_checker = VisualMatchChecker(local_only=False, api_key="fake-key", daily_cap=25)
    res = visual_checker.check_video("dummy.mp4", current_daily_calls=25)
    assert res["cap_reached"] is True
    assert res["review"] is True  # Cap reached defaults to review


# ==============================================================================
# 4. LAYER 5: PERCEPTUAL HASHING & HISTORY CLAIMS
# ==============================================================================

def test_history_claims_near_duplicate_detection():
    history_checker = HistoryClaimsChecker()

    # Synthesize two hashes with distance < 8
    # Format: phash:dhash where each is 16 hex chars (64-bit square hash)
    base_hash = "0" * 16 + ":" + "0" * 16
    near_hash = "0" * 15 + "1:" + "0" * 15 + "1"  # 1-bit difference (distance < 8)
    far_hash = "f" * 16 + ":" + "f" * 16           # completely different

    claimed_clips = [
        {
            "id": 1,
            "claimant": "CollabDRM",
            "asset_title": "Collab ViralHog - Test Video",
            "phashes_json": json.dumps([base_hash]),
        }
    ]

    # Near duplicate should match
    match = history_checker.check_history_duplicate(
        video_phashes=[near_hash],
        claimed_clips_rows=claimed_clips,
        threshold=8,
    )
    assert match is not None
    assert match["claimant"] == "CollabDRM"
    assert match["distance"] < 8

    # Far video should not match
    no_match = history_checker.check_history_duplicate(
        video_phashes=[far_hash],
        claimed_clips_rows=claimed_clips,
        threshold=8,
    )
    assert no_match is None


def test_generate_blocklist_suggestions():
    history_checker = HistoryClaimsChecker()
    suggestions = history_checker.generate_claim_suggestions({
        "id": 99,
        "asset_title": "Collab ViralHog - Female Pheasant Not Impressed By Mating Dance",
        "claimant": "CollabDRM",
        "claim_type": "both",
        "uploader": "suspicious_user",
        "source_name": "r/Unexpected",
    })

    values = [s["value"] for s in suggestions]
    assert "CollabDRM" in values
    assert "ViralHog" in values
    assert "suspicious_user" in values
    assert "Unexpected" in values


# ==============================================================================
# 5. ENGINE ORCHESTRATION & DECISION RULES
# ==============================================================================

def test_engine_hard_block_agency():
    engine = RightsCheckEngine()

    result = engine.evaluate_clip(
        post_id="test_post_1",
        video_path="",  # empty to skip local video sampling
        title="Cat jumping fail (Licensed by ViralHog)",
        uploader="test_user",
    )

    assert result["decision"] == "block"
    assert result["risk_score"] == 100
    assert "rights_agency" in result["reasons"]


def test_engine_score_capping_and_thresholds():
    engine = RightsCheckEngine()

    # When multiple flags accumulate, score must be capped at 100
    mock_ocr = MagicMock()
    mock_ocr.inspect_frames.return_value = {
        "agency_matches": [{"agency": "Storyful"}],
        "social_matches": [],
        "texts_seen": ["Storyful"],
        "hard_block_agency": True,
        "hard_block_social": True,
        "review_social": False,
    }
    engine.ocr_checker = mock_ocr

    res = engine.evaluate_clip(
        post_id="test_cap",
        video_path="dummy.mp4",
        title="Storyful Video over duration",
        duration=75.0,  # > 60s
    )
    assert res["risk_score"] <= 100
    assert res["decision"] == "block"


def test_engine_provider_error_review_policy():
    engine = RightsCheckEngine(config={"rights": {"on_provider_error": "review"}})

    # Triggering provider error on an otherwise clean clip must route to review, never silently pass
    mock_audio = MagicMock()
    mock_audio.check_audio.return_value = {
        "status": "error",
        "error": "Provider 500 error",
        "hard_block_music": False,
        "review_music": False,
        "provider_calls_made": 1,
    }
    engine.audio_checker = mock_audio

    res = engine.evaluate_clip(
        post_id="test_error",
        video_path="dummy.mp4",
        title="Clean innocent funny dog video",
    )
    assert res["decision"] == "review"
    assert "provider_error_review" in res["reasons"]


# ==============================================================================
# 6. YOUTUBE GATE & DATABASE TRANSACTION TESTS
# ==============================================================================

@pytest.fixture
def test_db():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_pipeline.db"
        db = AgentDatabase(db_path=db_path)
        yield db


def test_youtube_gate_unreviewed_hold(test_db):
    target_date = "2026-11-18"
    now_iso = datetime.now().isoformat()

    # Seed an unreviewed schedule clip
    with test_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO schedule (post_id, target_date, slot_hour, slot_time, video_path, created_at, rights_status, rights_risk)
            VALUES ('clip_hold_test', ?, 12, '2026-11-18T12:00:00-05:00', 'dummy.mp4', ?, 'needs_check', 45)
        """, (target_date, now_iso))
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, video_id, status, publish_at)
            VALUES ('clip_hold_test', ?, 'yt_vid_123', 'uploaded', '2026-11-18T12:00:00-05:00')
        """, (target_date,))

    mock_yt = MagicMock()
    gate = YouTubeRightsGate(db_path=test_db.db_path)
    res = gate.mark_held("clip_hold_test", youtube_service=mock_yt)

    assert res["success"] is True
    assert res["held"] is True
    assert mock_yt.videos().update.called

    # Verify DB status updated to held
    with test_db._get_connection() as conn:
        row = conn.execute("SELECT rights_status FROM schedule WHERE post_id = 'clip_hold_test'").fetchone()
        assert row["rights_status"] == "held"


def test_youtube_gate_mark_clear_and_reschedule(test_db):
    target_date = "2026-11-18"
    now_iso = datetime.now().isoformat()

    with test_db._get_connection() as conn:
        conn.execute("""
            INSERT INTO schedule (post_id, target_date, slot_hour, slot_time, video_path, created_at, rights_status)
            VALUES ('clip_clear_test', ?, 14, '2026-11-18T14:00:00-05:00', 'dummy.mp4', ?, 'needs_check')
        """, (target_date, now_iso))
        conn.execute("""
            INSERT INTO uploads (post_id, target_date, video_id, status)
            VALUES ('clip_clear_test', ?, 'yt_vid_clear', 'uploaded')
        """, (target_date,))

    mock_yt = MagicMock()
    gate = YouTubeRightsGate(db_path=test_db.db_path)
    res = gate.mark_clear("clip_clear_test", youtube_service=mock_yt)
    assert res["success"] is True
    assert res["publish_at"] is not None

    # Check that schedule rights_status is cleared
    with test_db._get_connection() as conn:
        row = conn.execute("SELECT rights_status FROM schedule WHERE post_id = 'clip_clear_test'").fetchone()
        assert row["rights_status"] == "cleared"


def test_pull_and_replace_transactional_safety(test_db):
    target_date = "2026-11-18"
    now_iso = datetime.now().isoformat()

    with tempfile.TemporaryDirectory() as td:
        dummy_file = Path(td) / "clean_reserve.mp4"
        dummy_file.write_bytes(b"dummy_video_bytes")

        with test_db._get_connection() as conn:
            conn.execute("""
                INSERT INTO schedule (post_id, target_date, slot_hour, slot_time, video_path, created_at, rights_status)
                VALUES ('claimed_post', ?, 17, '2026-11-18T17:00:00-05:00', 'dummy.mp4', ?, 'needs_check')
            """, (target_date, now_iso))
            conn.execute("""
                INSERT INTO uploads (post_id, target_date, video_id, status, slot_time, publish_at)
                VALUES ('claimed_post', ?, 'yt_vid_claimed', 'uploaded', '2026-11-18T17:00:00-05:00', '2026-11-18T17:00:00-05:00')
            """, (target_date,))
            conn.execute("""
                INSERT INTO reserve_pool (post_id, title, uploader, score, status, rights_status, video_path, added_at)
                VALUES ('reserve_clean', 'Fresh Reserve Clip', 'hero_user', 8.5, 'available', 'cleared', ?, ?)
            """, (str(dummy_file), now_iso))

        mock_yt = MagicMock()
        gate = YouTubeRightsGate(db_path=test_db.db_path)
        res = gate.replace_from_reserve(
            claimed_post_id="claimed_post",
            action_on_claimed="delete",
            delete_confirmation="DELETE",
            youtube_service=mock_yt,
        )

        assert res["success"] is True
        assert res["new_post_id"] == "reserve_clean"

        # Verify old video was deleted on YouTube
        mock_yt.videos().delete.assert_called_once_with(id="yt_vid_claimed")

        # Verify schedule and uploads were updated to reserve_clean
        with test_db._get_connection() as conn:
            sched = conn.execute("SELECT post_id, rights_status FROM schedule WHERE target_date = ?", (target_date,)).fetchone()
            assert sched["post_id"] == "reserve_clean"
            assert sched["rights_status"] == "cleared"

            res_row = conn.execute("SELECT status FROM reserve_pool WHERE post_id = 'reserve_clean'").fetchone()
            assert res_row["status"] == "used"


# ==============================================================================
# 7. SEEDED CLAIMS & SUBREDDIT PENALTY TESTS
# ==============================================================================

def test_seeded_claims_exist(test_db):
    """Verify that seeded claims reddit_1xv7kmf (kept, still public) and reddit_1xrfauk (pulled, deleted) are properly initialized."""
    claims = test_db.get_claims()
    claims_map = {c["post_id"]: c for c in claims}

    assert "reddit_1xv7kmf" in claims_map  # CollabDRM visual claim
    assert claims_map["reddit_1xv7kmf"]["action"] == "kept"

    assert "reddit_1xrfauk" in claims_map  # SME audio claim ("Eye of the Tiger")
    assert claims_map["reddit_1xrfauk"]["action"] == "pulled"


def test_unknown_subreddit_skips_penalty(test_db):
    """Unknown or empty subreddit must NOT receive fake high_claim_subreddit penalty."""
    engine = RightsCheckEngine(db=test_db)

    # Empty subreddit string
    res = engine.evaluate_clip(
        post_id="test_unknown_sub",
        video_path="",
        title="Just a clean video",
        subreddit="",
        source_url="",
    )
    assert "high_claim_subreddit" not in res["reasons"]
    assert res["risk_score"] == 0

    # None / unknown string
    res2 = engine.evaluate_clip(
        post_id="test_none_sub",
        video_path="",
        title="Just another video",
        subreddit="unknown",
        source_url=None,
    )
    assert "high_claim_subreddit" not in res2["reasons"]
    assert res2["risk_score"] == 0


def test_low_volume_subreddit_skips_penalty(test_db):
    """Subreddit with fewer than 20 real published clips must NOT receive claim penalty."""
    engine = RightsCheckEngine(db=test_db)

    # In test_db, r/funny has 2 claims seeded, but 0 real published clips (< 20 threshold)
    res = engine.evaluate_clip(
        post_id="test_funny_low_vol",
        video_path="",
        title="Funny clean clip",
        subreddit="r/funny",
        source_url="https://www.reddit.com/r/funny/comments/123/clip/",
    )
    assert "high_claim_subreddit" not in res["reasons"]
    assert res["risk_score"] == 0


def test_high_volume_high_claim_subreddit_applies_penalty(test_db):
    """Subreddit with >= 20 real published clips and > 10% claim rate receives penalty (+20)."""
    now_iso = datetime.now().isoformat()
    past_pub_at = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with test_db._get_connection() as conn:
        # Seed 25 published clips for r/risky (distinct dates/times)
        for i in range(25):
            pid = f"risky_pub_{i}"
            t_date = f"2026-10-{i+1:02d}"
            s_time = f"{t_date}T12:00:00-05:00"
            conn.execute("""
                INSERT INTO schedule (post_id, target_date, slot_hour, slot_time, video_path, created_at, source_url)
                VALUES (?, ?, 12, ?, 'dummy.mp4', ?, ?)
            """, (pid, t_date, s_time, now_iso, f"https://www.reddit.com/r/risky/comments/{pid}/"))
            conn.execute("""
                INSERT INTO uploads (post_id, target_date, video_id, status, publish_at)
                VALUES (?, ?, ?, 'scheduled', ?)
            """, (pid, t_date, f"yt_{pid}", past_pub_at))

        # Seed 5 claims for r/risky (5 / 25 = 20% > 10%)
        for i in range(5):
            conn.execute("""
                INSERT INTO claims (video_id, post_id, claim_type, claimant, asset_title, action, noted_at, source_name)
                VALUES (?, ?, 'audio', 'RecordCo', 'Track', 'kept', ?, 'r/risky')
            """, (f"yt_risky_pub_{i}", f"risky_pub_{i}", now_iso))

    engine = RightsCheckEngine(db=test_db)
    res = engine.evaluate_clip(
        post_id="test_risky_high_vol",
        video_path="",
        title="Risky clip",
        subreddit="r/risky",
        source_url="https://www.reddit.com/r/risky/comments/999/clip/",
    )
    assert "high_claim_subreddit" in res["reasons"]
    assert res["risk_score"] == 20


def test_count_real_published_logic(test_db):
    """Verify count_real_published accurately filters by publish time, status, and subreddit."""
    now_iso = datetime.now().isoformat()
    past_pub_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_pub_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with test_db._get_connection() as conn:
        # 1. Immediate uploaded
        conn.execute("INSERT INTO uploads (post_id, target_date, video_id, status) VALUES ('p_imm', '2026-11-10', 'yt_imm', 'uploaded')")
        # 2. Past scheduled publishAt
        conn.execute("INSERT INTO uploads (post_id, target_date, video_id, status, publish_at) VALUES ('p_past', '2026-11-10', 'yt_past', 'scheduled', ?)", (past_pub_at,))
        # 3. Future scheduled publishAt (must NOT be counted)
        conn.execute("INSERT INTO uploads (post_id, target_date, video_id, status, publish_at) VALUES ('p_fut', '2026-11-20', 'yt_fut', 'scheduled', ?)", (future_pub_at,))
        # 4. Deleted upload (must NOT be counted)
        conn.execute("INSERT INTO uploads (post_id, target_date, video_id, status, publish_at) VALUES ('p_del', '2026-11-10', 'yt_del', 'deleted', ?)", (past_pub_at,))
        # 5. Missing video_id (must NOT be counted)
        conn.execute("INSERT INTO uploads (post_id, target_date, video_id, status) VALUES ('p_novid', '2026-11-10', NULL, 'uploaded')")

    # Overall real published clips: 2 (p_imm and p_past)
    assert test_db.count_real_published() == 2


def test_shazam_provider_parses_match_no_match_and_errors(monkeypatch):
    import core.rights.audio_check as ac_mod
    from core.rights.audio_check import ShazamProvider

    monkeypatch.setattr(ac_mod, "_HAS_SHAZAMIO", True)
    monkeypatch.setattr(ShazamProvider, "MIN_INTERVAL_S", 0.0)
    provider = ShazamProvider()

    match = {
        "track": {
            "title": "Fortunate Son",
            "subtitle": "Creedence Clearwater Revival",
            "sections": [
                {"metadata": [
                    {"title": "Album", "text": "Willy and the Poor Boys"},
                    {"title": "Label", "text": "Fantasy"},
                    {"title": "Released", "text": "1969"},
                ]}
            ],
        }
    }

    async def fake_match(wav):
        return match

    monkeypatch.setattr(ShazamProvider, "_recognize_async", staticmethod(fake_match))
    res = provider.recognize(b"fake-wav")
    assert res["status"] == "success" and res["recognized"] is True
    assert res["title"] == "Fortunate Son"
    assert res["artist"] == "Creedence Clearwater Revival"
    assert res["label"] == "Fantasy"
    assert res["release_date"] == "1969"

    async def fake_no_match(wav):
        return {"matches": []}

    monkeypatch.setattr(ShazamProvider, "_recognize_async", staticmethod(fake_no_match))
    assert provider.recognize(b"fake-wav") == {"status": "success", "recognized": False}

    async def fake_boom(wav):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(ShazamProvider, "_recognize_async", staticmethod(fake_boom))
    res = provider.recognize(b"fake-wav")
    assert res["status"] == "error" and res["recognized"] is False
    assert "429" in res["error"]
