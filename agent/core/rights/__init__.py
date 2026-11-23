from .matcher import BlocklistMatcher, normalize_text, levenshtein_distance
from .ocr import VideoOCRChecker
from .audio_check import (
    AudioRightsChecker,
    LocalMusicDetector,
    ShazamProvider,
    AudioRecognitionProvider,
)
from .visual_match import VisualMatchChecker
from .perceptual import HistoryClaimsChecker, compute_video_phashes
from .engine import RightsCheckEngine
from .alerts import notify_rights_alert, log_alert, send_windows_notification

__all__ = [
    "BlocklistMatcher",
    "normalize_text",
    "levenshtein_distance",
    "VideoOCRChecker",
    "AudioRightsChecker",
    "LocalMusicDetector",
    "ShazamProvider",
    "AudioRecognitionProvider",
    "VisualMatchChecker",
    "HistoryClaimsChecker",
    "compute_video_phashes",
    "RightsCheckEngine",
    "notify_rights_alert",
    "log_alert",
    "send_windows_notification",
]
