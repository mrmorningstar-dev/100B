import os
import io
import time
import asyncio
import threading
import concurrent.futures
import csv
import av
import wave
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

try:
    import onnxruntime as ort
    _HAS_ONNX = True
except ImportError:
    _HAS_ONNX = False

try:
    from shazamio import Shazam
    _HAS_SHAZAMIO = True
except ImportError:
    _HAS_SHAZAMIO = False


# Music class indices from AudioSet / YAMNet
MUSIC_CLASS_INDEX = 132
MUSIC_SUBCLASS_INDICES = {
    132, 133, 147, 211, 212, 214, 222, 225, 228, 229, 232, 234, 235, 240,
    241, 242, 243, 244, 247, 248, 249, 251, 253, 254, 255, 256, 257, 259,
    260, 262, 263, 264, 265, 267, 268, 269, 270, 271, 272, 273, 274, 275, 276
}


def extract_audio_16k_mono(video_path: str) -> Tuple[np.ndarray, float]:
    """
    Extract audio as 16,000 Hz single-channel float32 array in [-1.0, 1.0].
    Returns (audio_array, duration_seconds).
    """
    if not os.path.isfile(video_path):
        return np.array([], dtype=np.float32), 0.0

    try:
        container = av.open(video_path)
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if not audio_stream:
            return np.array([], dtype=np.float32), 0.0

        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        samples = []
        for frame in container.decode(audio_stream):
            for resampled in resampler.resample(frame):
                samples.append(resampled.to_ndarray().flatten())

        if not samples:
            return np.array([], dtype=np.float32), 0.0

        audio_arr = np.concatenate(samples).astype(np.float32)
        duration = len(audio_arr) / 16000.0
        return audio_arr, duration
    except Exception as e:
        logger.debug(f"Audio extraction failed for {video_path}: {e}")
        return np.array([], dtype=np.float32), 0.0


def audio_to_wav_bytes(audio_segment: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode float32 audio segment to standard WAV byte buffer for API providers."""
    # Convert float32 [-1, 1] to int16 [-32768, 32767]
    clamped = np.clip(audio_segment, -1.0, 1.0)
    int16_audio = (clamped * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(int16_audio.tobytes())
    return buf.getvalue()


class LocalMusicDetector:
    """
    Layer 2A: Local YAMNet ONNX audio classifier.
    Computes seconds of music and fraction of clip containing music.
    """

    def __init__(self, model_path: Optional[Path] = None):
        if model_path is None:
            model_path = Path(__file__).resolve().parent.parent.parent / "data" / "models" / "yamnet.onnx"
        self.model_path = Path(model_path)
        self.session = None
        if _HAS_ONNX and self.model_path.is_file():
            try:
                self.session = ort.InferenceSession(str(self.model_path))
            except Exception as e:
                logger.warning(f"Failed to load YAMNet ONNX session: {e}")

    def detect_music(self, audio_arr: np.ndarray, duration: float) -> Dict[str, Any]:
        """
        Run inference across audio waveform.
        Returns music_seconds, music_fraction, has_music.
        """
        if self.session is None or len(audio_arr) < 8000:
            # Fallback if model not loaded or audio is empty
            return {
                "music_seconds": 0.0,
                "music_fraction": 0.0,
                "has_music": False,
                "model_active": False,
            }

        try:
            # YAMNet inference
            outputs = self.session.run(None, {"waveform": audio_arr})
            scores = outputs[0]  # shape: [N_frames, 521]

            # Check frames where music class or music sub-genres are confident (> 0.15)
            music_scores = scores[:, list(MUSIC_SUBCLASS_INDICES)]
            frame_max_music = np.max(music_scores, axis=1)
            music_frames = frame_max_music > 0.18

            # Each YAMNet frame corresponds to 0.48 seconds
            frame_hop = 0.48
            music_seconds = round(float(np.sum(music_frames)) * frame_hop, 2)
            music_seconds = min(music_seconds, duration)
            music_fraction = round(music_seconds / duration, 3) if duration > 0 else 0.0

            # Consider music present if at least 2.5s and > 8% of clip
            has_music = (music_seconds >= 2.5) and (music_fraction >= 0.08)

            return {
                "music_seconds": music_seconds,
                "music_fraction": music_fraction,
                "has_music": has_music,
                "model_active": True,
            }
        except Exception as e:
            logger.warning(f"YAMNet detection failed: {e}")
            return {
                "music_seconds": 0.0,
                "music_fraction": 0.0,
                "has_music": False,
                "model_active": False,
                "error": str(e),
            }


class AudioRecognitionProvider:
    """Abstract base class for commercial music fingerprint recognition providers."""

    def recognize(self, wav_bytes: bytes) -> Dict[str, Any]:
        raise NotImplementedError


class ShazamProvider(AudioRecognitionProvider):
    """
    Free music recognition via Shazam (unofficial `shazamio` library).
    No API key and no account needed. Shazam allows roughly 20 requests/minute,
    so calls are spaced out with a small minimum interval.
    """

    MIN_INTERVAL_S = 3.5
    REQUEST_TIMEOUT_S = 15.0

    _lock = threading.Lock()
    _last_call = 0.0

    def is_configured(self) -> bool:
        return _HAS_SHAZAMIO

    @staticmethod
    async def _recognize_async(wav_bytes: bytes) -> Dict[str, Any]:
        shazam = Shazam()
        try:
            return await shazam.recognize(wav_bytes)
        finally:
            close = getattr(shazam, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass

    @staticmethod
    def _meta(track: Dict[str, Any], name: str) -> Optional[str]:
        for section in track.get("sections") or []:
            for item in section.get("metadata") or []:
                if (item.get("title") or "").lower() == name.lower():
                    return item.get("text")
        return None

    def recognize(self, wav_bytes: bytes) -> Dict[str, Any]:
        res = self._recognize_once(wav_bytes)
        if res.get("status") == "error":
            # One retry: Shazam occasionally stalls or drops a request.
            res = self._recognize_once(wav_bytes)
        return res

    def _recognize_once(self, wav_bytes: bytes) -> Dict[str, Any]:
        if not self.is_configured():
            return {"status": "unconfigured", "recognized": False}

        try:
            with self._lock:
                wait = self.MIN_INTERVAL_S - (time.monotonic() - ShazamProvider._last_call)
                if wait > 0:
                    time.sleep(wait)
                try:
                    # Run in its own thread + event loop so this also works
                    # when the caller already has a running event loop.
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        fut = pool.submit(lambda: asyncio.run(self._recognize_async(wav_bytes)))
                        out = fut.result(timeout=self.REQUEST_TIMEOUT_S)
                    finally:
                        # Do not wait for a stalled request; let it finish in the background.
                        pool.shutdown(wait=False)
                finally:
                    ShazamProvider._last_call = time.monotonic()

            track = (out or {}).get("track")
            if not track:
                return {"status": "success", "recognized": False}

            return {
                "status": "success",
                "recognized": True,
                "title": track.get("title"),
                "artist": track.get("subtitle"),
                "label": self._meta(track, "Label") or self._meta(track, "Album"),
                "release_date": self._meta(track, "Released"),
                "score": 100,
            }
        except Exception as e:
            logger.warning(f"Shazam recognition failed: {e}")
            return {"status": "error", "recognized": False, "error": str(e) or type(e).__name__, "error_code": None}


class AudioRightsChecker:
    """
    Layer 2 Orchestrator:
      - 2A: Local YAMNet music detection
      - 2B: Track fingerprinting with Shazam (free) (1 sample first, 2nd only if music-heavy)
    """

    def __init__(
        self,
        local_detector: Optional[LocalMusicDetector] = None,
        provider: Optional[AudioRecognitionProvider] = None,
        daily_cap: int = 40,
        multi_sample_threshold: float = 0.60,
    ):
        self.detector = local_detector or LocalMusicDetector()
        self.provider = provider or ShazamProvider()
        self.daily_cap = daily_cap
        self.multi_sample_threshold = multi_sample_threshold

    def get_audio_samples(self, audio_arr: np.ndarray, duration: float) -> List[Tuple[float, np.ndarray]]:
        """
        Extract up to two 12-second audio windows:
          Sample 1: Centered window around middle / peak
          Sample 2: Offset window (if music heavy)
        """
        samples = []
        sample_len = 12 * 16000  # 12 seconds
        total_len = len(audio_arr)

        if total_len <= sample_len:
            samples.append((0.0, audio_arr))
            return samples

        # Sample 1: middle of the clip
        mid_start = max(0, (total_len - sample_len) // 2)
        samples.append((mid_start / 16000.0, audio_arr[mid_start : mid_start + sample_len]))

        # Sample 2: earlier or later window
        if mid_start >= sample_len:
            alt_start = 0
        else:
            alt_start = min(total_len - sample_len, mid_start + sample_len // 2)

        if alt_start != mid_start:
            samples.append((alt_start / 16000.0, audio_arr[alt_start : alt_start + sample_len]))

        return samples

    def check_audio(
        self,
        video_path: str,
        current_daily_calls: int = 0,
        cache_lookup_fn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Perform 2-step audio check.
        Returns:
          - music_seconds, music_fraction, has_music
          - recognized_track: Optional[Dict]
          - hard_block_music: bool
          - review_music: bool
          - provider_calls_made: int
        """
        safe_calls = current_daily_calls if isinstance(current_daily_calls, (int, float)) else 0
        if safe_calls >= self.daily_cap:
            logger.warning("Fingerprint daily call cap reached. Skipping provider fingerprinting.")
            return {
                "music_seconds": 0.0,
                "music_fraction": 0.0,
                "has_music": False,
                "hard_block_music": False,
                "review_music": True,
                "provider_calls_made": 0,
                "cap_reached": True,
                "status": "cap_reached",
            }

        audio_arr, duration = extract_audio_16k_mono(video_path)
        if len(audio_arr) == 0 or duration < 1.0:
            return {
                "music_seconds": 0.0,
                "music_fraction": 0.0,
                "has_music": False,
                "hard_block_music": False,
                "review_music": False,
                "provider_calls_made": 0,
                "cap_reached": False,
                "status": "no_audio",
            }

        # Step 2A: Local YAMNet detection
        local_res = self.detector.detect_music(audio_arr, duration)
        music_seconds = local_res["music_seconds"]
        music_fraction = local_res["music_fraction"]
        has_music = local_res["has_music"]

        # If speech or ambient only, pass immediately with 0 provider calls
        if not has_music or music_seconds < 2.5:
            return {
                "music_seconds": music_seconds,
                "music_fraction": music_fraction,
                "has_music": False,
                "hard_block_music": False,
                "review_music": False,
                "provider_calls_made": 0,
                "status": "clean_speech_or_ambient",
            }

        # Step 2B: Provider fingerprinting
        samples = self.get_audio_samples(audio_arr, duration)
        calls_made = 0
        provider_errors = 0
        last_error_msg = None
        last_error_code = None
        recognized_track = None

        for idx, (offset_s, sample_arr) in enumerate(samples):
            # Optimization rule: Send 1 sample first, send second ONLY if unrecognized AND music-heavy (>= threshold)
            if idx == 1:
                if recognized_track is not None:
                    break
                if music_fraction < self.multi_sample_threshold:
                    break

            if safe_calls + calls_made >= self.daily_cap:
                logger.warning("Fingerprint daily call cap reached. Skipping further fingerprinting.")
                break

            wav_bytes = audio_to_wav_bytes(sample_arr)

            # Check cache if lookup provided
            cached_res = cache_lookup_fn(wav_bytes) if cache_lookup_fn else None
            if cached_res:
                track_info = cached_res
            else:
                calls_made += 1
                track_info = self.provider.recognize(wav_bytes)

            if track_info.get("status") == "error":
                provider_errors += 1
                last_error_msg = track_info.get("error", "Fingerprint provider error")
                last_error_code = track_info.get("error_code")
                logger.warning(f"Fingerprint recognition error: {last_error_msg} (code: {last_error_code})")
                # Do not call second sample if first errored
                break

            if track_info.get("recognized"):
                recognized_track = track_info
                break

        # Decision rule:
        # 1. Hard block if commercial track recognized (> threshold)
        hard_block_music = bool(recognized_track and recognized_track.get("recognized"))

        # 2. Check provider error
        has_provider_error = bool(provider_errors > 0)

        # 3. Review if music detected locally (> 4.5s or > 25% of clip)
        review_music = False
        if not hard_block_music and has_music and (music_seconds > 4.5 or music_fraction > 0.25):
            review_music = True

        if has_provider_error:
            status = "error"
        elif hard_block_music:
            status = "recognized"
        elif review_music:
            status = "unrecognized_music"
        else:
            status = "pass"

        return {
            "music_seconds": music_seconds,
            "music_fraction": music_fraction,
            "has_music": has_music,
            "recognized_track": recognized_track,
            "hard_block_music": hard_block_music,
            "review_music": review_music,
            "provider_calls_made": calls_made,
            "provider_errors": provider_errors,
            "provider_error_msg": last_error_msg,
            "provider_error_code": last_error_code,
            "cap_reached": (safe_calls + calls_made >= self.daily_cap),
            "status": status,
            "error": last_error_msg if has_provider_error else None,
        }
