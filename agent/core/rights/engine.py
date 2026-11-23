import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger

from .matcher import BlocklistMatcher
from .ocr import VideoOCRChecker
from .audio_check import AudioRightsChecker
from .visual_match import VisualMatchChecker
from .perceptual import compute_video_phashes, HistoryClaimsChecker


class RightsCheckEngine:
    """
    Five-layer Content ID and rights risk evaluation engine.
    Orchestrates Layer 1 (Agency & OCR), Layer 2 (Audio & Fingerprint),
    Layer 3 (Cloud Vision Web Match), Layer 4 (Scheduled Gate), and Layer 5 (Claims History).
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
        audio_checker: Optional[AudioRightsChecker] = None,
        ocr_checker: Optional[VideoOCRChecker] = None,
        visual_checker: Optional[VisualMatchChecker] = None,
    ):
        self.config = config or {}
        rights_cfg = self.config.get("rights", {})
        self.db = db

        self.matcher = BlocklistMatcher()
        self.ocr_checker = ocr_checker or VideoOCRChecker(matcher=self.matcher)
        self.audio_checker = audio_checker or AudioRightsChecker(
            daily_cap=rights_cfg.get("providers", {}).get("shazam", {}).get("daily_cap", 40),
            multi_sample_threshold=rights_cfg.get("providers", {}).get("shazam", {}).get("multi_sample_threshold", 0.60),
        )

        is_local_only = rights_cfg.get("local_only", True)
        self.visual_checker = visual_checker or VisualMatchChecker(
            local_only=is_local_only,
            daily_cap=rights_cfg.get("providers", {}).get("google_vision", {}).get("daily_cap", 25),
            budget_alert_threshold=rights_cfg.get("providers", {}).get("google_vision", {}).get("budget_alert_threshold", 800),
            matcher=self.matcher,
        )

        self.history_checker = HistoryClaimsChecker()
        self.weights = rights_cfg.get("weights", {
            "hard_block_score": 100,
            "unrecognized_music": 35,
            "high_web_matches": 30,
            "duration_over_60s": 15,
            "high_claim_subreddit": 20,
            "social_watermark_review": 40,
        })
        self.thresholds = rights_cfg.get("thresholds", {
            "block": 80,
            "review": 30,
        })
        self.prefer_max_seconds = float(rights_cfg.get("prefer_max_seconds", 60.0))
        self.on_provider_error = rights_cfg.get("on_provider_error", "review")
        self._alerted_providers = set()

    def _trigger_provider_error_alert(self, provider_name: str, error_detail: str) -> None:
        """Trigger desktop notification and log alert once per provider."""
        prov_key = provider_name.lower()
        if prov_key not in self._alerted_providers:
            self._alerted_providers.add(prov_key)
            msg = f"{provider_name} returned error: {error_detail}. Music-heavy clips routed to review."
            try:
                from .alerts import notify_rights_alert
                notify_rights_alert(f"{provider_name} Provider Error", msg)
            except Exception as e:
                logger.warning(f"Could not dispatch provider error alert: {e}")

    def evaluate_clip(
        self,
        post_id: str,
        video_path: str,
        title: str = "",
        uploader: str = "",
        subreddit: str = "",
        source_url: str = "",
        raw_video_path: Optional[str] = None,
        crosspost_parent: Optional[str] = None,
        vision_judge_names: Optional[List[str]] = None,
        target_date: Optional[str] = None,
        duration: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Run full 5-layer evaluation on a candidate clip.
        Returns complete decision, score (0-100), reasons, and layer details.
        """
        checked_at = datetime.now().isoformat()
        target_date = target_date or datetime.now().strftime("%Y-%m-%d")

        layers_result: Dict[str, Any] = {}
        reasons: List[str] = []
        risk_score = 0
        has_hard_block = False

        # 1. Normalize and resolve subreddit
        clean_sub = None
        if subreddit:
            s_candidate = subreddit.strip().replace("r/", "").replace("/r/", "").strip()
            if s_candidate and s_candidate.lower() not in ("unknown", "none", ""):
                clean_sub = s_candidate

        # If not provided directly, attempt to extract from source_url
        if not clean_sub and source_url:
            m = re.search(r"reddit\.com/r/([^/?#]+)", source_url, re.IGNORECASE)
            if not m:
                m = re.search(r"/r/([^/?#]+)", source_url, re.IGNORECASE)
            if m:
                s_extracted = m.group(1).strip()
                if s_extracted and s_extracted.lower() not in ("unknown", "none", ""):
                    clean_sub = s_extracted

        # Retrieve active learned blocklist and past claims from DB if available
        active_blocklist: List[Dict[str, Any]] = []
        claimed_clips_rows: List[Dict[str, Any]] = []
        subreddit_claim_rate = 0.0

        if self.db and hasattr(self.db, "_get_connection"):
            try:
                with self.db._get_connection() as conn:
                    # Active learned blocklist
                    cur = conn.execute("SELECT * FROM rights_blocklist WHERE status = 'active'")
                    fetched_bl = cur.fetchall()
                    if fetched_bl and hasattr(fetched_bl[0], "keys"):
                        active_blocklist = [dict(r) for r in fetched_bl]

                    # Past claims with phashes
                    cur = conn.execute("SELECT id, claimant, asset_title, phashes_json FROM claims WHERE phashes_json IS NOT NULL")
                    fetched_claims = cur.fetchall()
                    if fetched_claims and hasattr(fetched_claims[0], "keys"):
                        claimed_clips_rows = [dict(r) for r in fetched_claims]

                    # Subreddit claim stats:
                    # Skip penalty when subreddit is unknown, and require at least 20 real published clips from that subreddit first
                    if clean_sub:
                        sub_published = 0
                        if hasattr(self.db, "count_real_published"):
                            sub_published = self.db.count_real_published(subreddit=clean_sub)
                        else:
                            now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            today_ny = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                            q_sub_pub = """
                                SELECT COUNT(*) FROM uploads u
                                LEFT JOIN schedule s ON u.post_id = s.post_id
                                WHERE u.video_id IS NOT NULL AND u.status != 'deleted'
                                  AND (
                                    u.status IN ('uploaded', 'published')
                                    OR (u.publish_at IS NOT NULL AND u.publish_at <= ?)
                                    OR (u.publish_at IS NULL AND u.target_date <= ?)
                                  )
                                  AND (
                                    LOWER(s.source_url) LIKE ?
                                    OR LOWER(s.source_url) LIKE ?
                                  )
                            """
                            row_pub = conn.execute(q_sub_pub, (
                                now_utc_str,
                                today_ny,
                                f"%reddit.com/r/{clean_sub.lower()}/%",
                                f"%/r/{clean_sub.lower()}/%",
                            )).fetchone()
                            sub_published = row_pub[0] if row_pub and isinstance(row_pub[0], (int, float)) else 0

                        # Require at least 20 real published clips from that subreddit first
                        if isinstance(sub_published, (int, float)) and sub_published >= 20:
                            res_claims = conn.execute(
                                "SELECT COUNT(*) FROM claims WHERE LOWER(source_name) LIKE ?",
                                (f"%{clean_sub.lower()}%",)
                            ).fetchone()
                            if res_claims and isinstance(res_claims[0], (int, float)):
                                subreddit_claim_rate = res_claims[0] / sub_published
            except Exception as e:
                logger.debug(f"DB access error in rights check: {e}")

        # ----------------------------------------------------------------------
        # LAYER 1: Agency and Source Blocklist (Local, Free)
        # ----------------------------------------------------------------------
        l1_agency_matches = []
        l1_watermark_matches = []

        # 1. Text checks (title, crosspost, domain)
        combined_text = f"{title} {crosspost_parent or ''}"
        ag_match = self.matcher.check_agency_match(combined_text)
        if ag_match:
            l1_agency_matches.append({"agency": ag_match[0], "matched_in": "title/crosspost"})

        domain_match = self.matcher.check_domain_match(source_url)
        if domain_match:
            l1_agency_matches.append({"agency": domain_match, "matched_in": "source_domain"})

        # 2. Check learned blocklist
        learned_hit = self.matcher.check_learned_blocklist(
            active_blocklist, title=title, uploader=uploader, subreddit=subreddit
        )
        if learned_hit:
            l1_agency_matches.append({
                "agency": f"Learned ({learned_hit.get('type')}: {learned_hit.get('value')})",
                "matched_in": "learned_blocklist",
            })

        # 3. OCR on video frames
        ocr_res = self.ocr_checker.inspect_frames(video_path)
        if ocr_res.get("hard_block_agency"):
            for m in ocr_res.get("agency_matches", []):
                l1_agency_matches.append({"agency": m.get("agency"), "matched_in": f"ocr_frame_{m.get('frame')}"})

        # 4. Social watermark rules (hard block only if in corner or in 2+ frames, otherwise review)
        if ocr_res.get("hard_block_social"):
            l1_watermark_matches.append("corner_or_multi_frame_watermark")
            reasons.append("rights_social_watermark")
            has_hard_block = True
            risk_score += self.weights.get("hard_block_score", 100)
        elif ocr_res.get("review_social"):
            reasons.append("rights_social_watermark_review")
            risk_score += self.weights.get("social_watermark_review", 40)

        # 5. Vision Judge names check
        if vision_judge_names:
            for v_name in vision_judge_names:
                ag_v = self.matcher.check_agency_match(v_name)
                if ag_v:
                    l1_agency_matches.append({"agency": ag_v[0], "matched_in": "vision_judge_llm"})

        if l1_agency_matches:
            reasons.append("rights_agency")
            has_hard_block = True
            risk_score += self.weights.get("hard_block_score", 100)

        layers_result["layer1_agency"] = {
            "status": "block" if l1_agency_matches or ocr_res.get("hard_block_social") else ("review" if ocr_res.get("review_social") else "pass"),
            "agency_matches": l1_agency_matches,
            "social_watermarks": ocr_res.get("social_matches", []),
            "texts_seen_sample": ocr_res.get("texts_seen", [])[:8],
        }

        # ----------------------------------------------------------------------
        # LAYER 2: Audio Check
        # ----------------------------------------------------------------------
        # Lookup provider calls usage for today
        cur_calls = 0
        if self.db and hasattr(self.db, "get_provider_usage"):
            try:
                usage = self.db.get_provider_usage(provider="shazam")
                cur_calls = usage.get("calls", 0)
            except Exception:
                cur_calls = 0

        audio_res = self.audio_checker.check_audio(
            video_path=video_path,
            current_daily_calls=cur_calls,
        )

        # Record calls made and errors in DB if any
        calls_count = audio_res.get("provider_calls_made", 0)
        errors_count = audio_res.get("provider_errors", 0)
        if (calls_count > 0 or errors_count > 0) and self.db and hasattr(self.db, "increment_provider_usage"):
            try:
                self.db.increment_provider_usage("shazam", count=calls_count, errors=errors_count)
            except Exception as e:
                logger.warning(f"Failed to increment provider usage: {e}")

        if audio_res.get("hard_block_music"):
            reasons.append("rights_music")
            has_hard_block = True
            risk_score += self.weights.get("hard_block_score", 100)
        elif audio_res.get("status") == "error":
            reasons.append("provider_error_review")
            risk_score += self.weights.get("unrecognized_music", 35)
            self._trigger_provider_error_alert("Shazam", audio_res.get("error", "Shazam recognition error"))
        elif audio_res.get("review_music"):
            reasons.append("rights_music_unrecognized")
            risk_score += self.weights.get("unrecognized_music", 35)

        layers_result["layer2_audio"] = audio_res

        # ----------------------------------------------------------------------
        # LAYER 3: Visual Match Check (Web Detection)
        # ----------------------------------------------------------------------
        raw_target = raw_video_path or video_path
        cur_vision_calls = 0
        if self.db and hasattr(self.db, "get_provider_usage"):
            try:
                usage_v = self.db.get_provider_usage(provider="google_vision")
                cur_vision_calls = usage_v.get("calls", 0)
            except Exception:
                cur_vision_calls = 0

        vision_res = self.visual_checker.check_video(
            video_path=raw_target,
            current_daily_calls=cur_vision_calls,
        )

        if vision_res.get("calls_made", 0) > 0 and self.db and hasattr(self.db, "increment_provider_usage"):
            try:
                self.db.increment_provider_usage("google_vision", count=vision_res["calls_made"])
            except Exception:
                pass

        if vision_res.get("hard_block"):
            reasons.append("rights_visual_match")
            has_hard_block = True
            risk_score += self.weights.get("hard_block_score", 100)
        elif vision_res.get("review"):
            reasons.append("rights_visual_review")
            risk_score += self.weights.get("high_web_matches", 30)

        layers_result["layer3_visual"] = vision_res

        # ----------------------------------------------------------------------
        # LAYER 5: Perceptual Hash History Match
        # ----------------------------------------------------------------------
        video_phashes = compute_video_phashes(video_path, num_frames=3)
        history_match = self.history_checker.check_history_duplicate(video_phashes, claimed_clips_rows, threshold=8)

        if history_match:
            reasons.append("rights_history")
            has_hard_block = True
            risk_score += self.weights.get("hard_block_score", 100)

        layers_result["layer5_history"] = {
            "duplicate_match": bool(history_match),
            "match_details": history_match,
            "phashes": video_phashes,
        }

        # ----------------------------------------------------------------------
        # SOFT FACTORS: Duration > 60s & Subreddit Claim Rate
        # ----------------------------------------------------------------------
        clip_dur = duration or audio_res.get("music_seconds", 0)
        if clip_dur > self.prefer_max_seconds:
            reasons.append("duration_over_60s")
            risk_score += self.weights.get("duration_over_60s", 15)

        if clean_sub and subreddit_claim_rate > 0.10:
            reasons.append("high_claim_subreddit")
            risk_score += self.weights.get("high_claim_subreddit", 20)

        # ----------------------------------------------------------------------
        # AGGREGATE DECISION & SCORE (Capped at 100)
        # ----------------------------------------------------------------------
        final_score = min(100, max(0, risk_score))

        if has_hard_block or final_score >= self.thresholds.get("block", 80):
            decision = "block"
        elif final_score >= self.thresholds.get("review", 30):
            decision = "review"
        else:
            decision = "pass"

        # Safe fallback on provider error
        if self.on_provider_error == "review" and decision != "block":
            if audio_res.get("status") == "error" or vision_res.get("status") == "error":
                decision = "review"
                if "provider_error_review" not in reasons:
                    reasons.append("provider_error_review")

        evaluation_result = {
            "post_id": post_id,
            "target_date": target_date,
            "checked_at": checked_at,
            "layers": layers_result,
            "risk_score": final_score,
            "decision": decision,
            "reasons": reasons,
            "video_phashes": video_phashes,
        }

        # Record evaluation in SQLite if DB instance is provided
        if self.db and hasattr(self.db, "record_rights_check"):
            try:
                self.db.record_rights_check(
                    post_id=post_id,
                    target_date=target_date,
                    layers=layers_result,
                    risk_score=final_score,
                    decision=decision,
                    reasons=reasons,
                )
            except Exception as e:
                logger.debug(f"Failed to record rights check in DB: {e}")

        return evaluation_result
