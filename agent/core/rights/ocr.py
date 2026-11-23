import os
import cv2
import numpy as np
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from loguru import logger

from .matcher import BlocklistMatcher, normalize_text

try:
    from rapidocr_onnxruntime import RapidOCR
    _HAS_RAPID_OCR = True
except ImportError:
    _HAS_RAPID_OCR = False


class VideoOCRChecker:
    """
    Layer 1 OCR: Samples 4-5 frames from a video, detects text, and evaluates agency
    and social watermark matches.
    """

    def __init__(self, matcher: Optional[BlocklistMatcher] = None):
        self.matcher = matcher or BlocklistMatcher()
        self.ocr = RapidOCR() if _HAS_RAPID_OCR else None

    def sample_frames(self, video_path: str, num_frames: int = 5) -> List[np.ndarray]:
        """Sample num_frames evenly distributed across video duration."""
        if not video_path or not os.path.isfile(video_path):
            return []
        frames = []
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return frames

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return frames

        step = max(1, total_frames // (num_frames + 1))
        indices = [step * (i + 1) for i in range(num_frames)]

        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total_frames - 1))
            ret, frame = cap.read()
            if ret and frame is not None:
                frames.append(frame)

        cap.release()
        return frames

    def is_in_corner(self, box: List[List[float]], img_width: int, img_height: int) -> bool:
        """
        Check if bounding box center lies in any of the 4 video corners.
        Corner regions: horizontal outer 35%, vertical outer 25%.
        """
        if not box or img_width <= 0 or img_height <= 0:
            return False

        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)

        norm_x = cx / img_width
        norm_y = cy / img_height

        in_x_outer = (norm_x < 0.35) or (norm_x > 0.65)
        in_y_outer = (norm_y < 0.25) or (norm_y > 0.75)

        return in_x_outer and in_y_outer

    def inspect_frames(self, video_path: str) -> Dict[str, Any]:
        """
        Run OCR across 4-5 video frames.
        Returns detailed findings:
          - agency_matches: list of agency detections
          - social_matches: list of social watermark detections
          - texts_seen: all detected text snippets
          - hard_block_social: boolean (true if in corner or in 2+ frames)
          - review_social: boolean
        """
        if not self.ocr:
            logger.warning("RapidOCR not installed. Skipping local OCR.")
            return {
                "agency_matches": [],
                "social_matches": [],
                "texts_seen": [],
                "hard_block_agency": False,
                "hard_block_social": False,
                "review_social": False,
            }

        frames = self.sample_frames(video_path, num_frames=5)
        if not frames:
            return {
                "agency_matches": [],
                "social_matches": [],
                "texts_seen": [],
                "hard_block_agency": False,
                "hard_block_social": False,
                "review_social": False,
            }

        agency_matches = []
        social_matches = []
        all_texts = []
        social_frames_count = 0
        has_corner_social = False

        for frame_idx, frame in enumerate(frames):
            h, w = frame.shape[:2]
            try:
                result, _ = self.ocr(frame)
            except Exception as e:
                logger.debug(f"OCR error on frame {frame_idx}: {e}")
                continue

            if not result:
                continue

            frame_has_social = False
            for item in result:
                # item: [box, text, score]
                box = item[0]
                text = item[1]
                score = float(item[2]) if len(item) > 2 else 1.0

                if score < 0.40 or not text:
                    continue

                all_texts.append(text)

                # 1. Agency match (Hard block)
                ag_match = self.matcher.check_agency_match(text)
                if ag_match:
                    agency_matches.append({
                        "agency": ag_match[0],
                        "alias": ag_match[1],
                        "text": text,
                        "frame": frame_idx,
                    })

                # 2. Social watermark match
                soc_match = self.matcher.check_social_match(text)
                if soc_match:
                    frame_has_social = True
                    in_corner = self.is_in_corner(box, w, h)
                    if in_corner:
                        has_corner_social = True
                    social_matches.append({
                        "platform": soc_match[0],
                        "alias": soc_match[1],
                        "text": text,
                        "frame": frame_idx,
                        "in_corner": in_corner,
                    })

            if frame_has_social:
                social_frames_count += 1

        # Rule: Hard-block social watermark text only if in a corner or in 2+ frames, otherwise review
        hard_block_social = has_corner_social or (social_frames_count >= 2)
        review_social = bool(social_matches) and not hard_block_social

        return {
            "agency_matches": agency_matches,
            "social_matches": social_matches,
            "texts_seen": all_texts,
            "hard_block_agency": bool(agency_matches),
            "hard_block_social": hard_block_social,
            "review_social": review_social,
        }
