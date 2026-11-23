import os
import cv2
import base64
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger

from .matcher import BlocklistMatcher


class VisualMatchChecker:
    """
    Layer 3: Google Cloud Vision Web Detection (WEB_DETECTION).
    Detects matching images and pages across the web on 2 raw keyframes.
    Supports Local-Only mode, daily quota caps, and domain scoring.
    """

    def __init__(
        self,
        local_only: bool = True,
        api_key: Optional[str] = None,
        daily_cap: int = 25,
        budget_alert_threshold: int = 800,
        matcher: Optional[BlocklistMatcher] = None,
    ):
        self.local_only = local_only
        self.api_key = api_key or os.environ.get("GOOGLE_VISION_API_KEY", "")
        self.daily_cap = daily_cap
        self.budget_alert_threshold = budget_alert_threshold
        self.matcher = matcher or BlocklistMatcher()

    def extract_raw_keyframes(self, video_path: str, count: int = 2) -> List[bytes]:
        """Extract JPEG bytes of count raw keyframes evenly spaced."""
        frames_bytes = []
        if not os.path.isfile(video_path):
            return frames_bytes

        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            cap.release()
            return frames_bytes

        indices = [int(total * 0.33), int(total * 0.66)] if count == 2 else [int(total * 0.5)]

        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret and frame is not None:
                # Resize to max 640px to conserve bandwidth
                h, w = frame.shape[:2]
                if max(h, w) > 640:
                    scale = 640.0 / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                ret2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ret2:
                    frames_bytes.append(buf.tobytes())

        cap.release()
        return frames_bytes

    def query_web_detection(self, image_bytes: bytes) -> Dict[str, Any]:
        """Call Google Cloud Vision WEB_DETECTION endpoint."""
        if self.local_only or not self.api_key:
            return {"status": "local_only", "full_matches": 0, "partial_matches": 0, "pages": []}

        try:
            b64_content = base64.b64encode(image_bytes).decode("utf-8")
            url = f"https://vision.googleapis.com/v1/images:annotate?key={self.api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": b64_content},
                        "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
                    }
                ]
            }

            resp = requests.post(url, json=payload, timeout=12.0)
            if not resp.ok:
                return {"status": "error", "error": f"HTTP {resp.status_code}"}

            data = resp.json()
            responses = data.get("responses", [])
            if not responses:
                return {"status": "success", "full_matches": 0, "partial_matches": 0, "pages": []}

            web = responses[0].get("webDetection", {})
            full_matches = len(web.get("fullMatchingImages", []))
            partial_matches = len(web.get("partialMatchingImages", []))
            pages = []
            for p in web.get("pagesWithMatchingImages", []):
                p_url = p.get("url", "")
                if p_url:
                    pages.append(p_url)

            return {
                "status": "success",
                "full_matches": full_matches,
                "partial_matches": partial_matches,
                "pages": pages,
            }
        except Exception as e:
            logger.warning(f"Google Cloud Vision call failed: {e}")
            return {"status": "error", "error": str(e)}

    def check_video(
        self,
        video_path: str,
        current_daily_calls: int = 0,
    ) -> Dict[str, Any]:
        """
        Check raw video keyframes against Web Detection.
        Evaluates matches and scores risk.
        """
        if self.local_only:
            return {
                "active": False,
                "local_only": True,
                "hard_block": False,
                "review": False,
                "agency_matches": [],
                "matched_domains": [],
                "calls_made": 0,
            }

        safe_calls = current_daily_calls if isinstance(current_daily_calls, (int, float)) else 0
        if safe_calls >= self.daily_cap:
            logger.warning("Google Cloud Vision daily cap reached.")
            return {
                "active": False,
                "cap_reached": True,
                "hard_block": False,
                "review": True,
                "agency_matches": [],
                "matched_domains": [],
                "calls_made": 0,
            }

        frames = self.extract_raw_keyframes(video_path, count=2)
        if not frames:
            return {
                "active": True,
                "hard_block": False,
                "review": False,
                "agency_matches": [],
                "matched_domains": [],
                "calls_made": 0,
            }

        calls_made = 0
        all_pages = []
        agency_hits = []
        matched_domains = set()

        for f_bytes in frames:
            if current_daily_calls + calls_made >= self.daily_cap:
                break
            calls_made += 1
            res = self.query_web_detection(f_bytes)
            if res.get("status") == "success":
                pages = res.get("pages", [])
                all_pages.extend(pages)
                for p in pages:
                    matched_agency = self.matcher.check_domain_match(p)
                    if matched_agency:
                        agency_hits.append({"agency": matched_agency, "url": p})
                    try:
                        domain = p.split("/")[2].lower()
                        matched_domains.add(domain)
                    except Exception:
                        pass

        # Domain scoring:
        # 1. Hard block if any agency domain matched
        hard_block = bool(agency_hits)

        # 2. Review if high match count on other video platforms (YouTube / TikTok / stock)
        # Exclude benign Reddit / Imgur social matches
        video_platform_domains = {"youtube.com", "youtu.be", "tiktok.com", "dailymotion.com", "vimeo.com"}
        non_reddit_domains = [d for d in matched_domains if "reddit.com" not in d and "redd.it" not in d and "imgur.com" not in d]
        has_video_platforms = any(any(vp in d for vp in video_platform_domains) for d in non_reddit_domains)
        review = (not hard_block) and (len(non_reddit_domains) >= 3 or has_video_platforms)

        return {
            "active": True,
            "local_only": False,
            "hard_block": hard_block,
            "review": review,
            "agency_matches": agency_hits,
            "matched_domains": list(matched_domains),
            "calls_made": calls_made,
        }
