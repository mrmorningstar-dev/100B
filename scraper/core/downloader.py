import os
import re
import json
import shutil
import subprocess
import urllib.parse
from urllib.parse import urljoin
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import requests
from loguru import logger
import yt_dlp

from core.db import VideoDatabase
from core.dedupe import Deduplicator
from core.filters import check_media_post_download
from core.captions import clean_caption, llm_generate_caption


def probe_media_file(filepath: Path) -> Dict[str, Any]:
    """Inspect downloaded video using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(filepath)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"duration": 0.0, "width": 0, "height": 0, "has_audio": False}

    try:
        data = json.loads(res.stdout)
        width, height = 0, 0
        has_audio = False

        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and width == 0:
                width = int(s.get("width", 0))
                height = int(s.get("height", 0))
            elif s.get("codec_type") == "audio":
                has_audio = True

        duration = float(data.get("format", {}).get("duration", 0.0))
        return {
            "duration": duration,
            "width": width,
            "height": height,
            "has_audio": has_audio,
        }
    except Exception as e:
        logger.warning(f"Error parsing ffprobe output for {filepath}: {e}")
        return {"duration": 0.0, "width": 0, "height": 0, "has_audio": False}


def probe_hls_duration_http(playlist_url: str, timeout: int = 5) -> Optional[float]:
    """
    Fetch an HLS playlist via standard HTTP request, pick a variant stream if master,
    and add up the #EXTINF segment lengths to determine total duration in under a second.
    """
    headers = {
        "User-Agent": os.getenv(
            "REDDIT_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
    }
    if "YOUR_USERNAME" in headers["User-Agent"]:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    try:
        resp = requests.get(playlist_url, headers=headers, timeout=timeout)
        if resp.status_code != 200 or not resp.text:
            return None

        text = resp.text

        # 1. Direct media playlist: check for #EXTINF segment tags directly
        extinfs = [float(x) for x in re.findall(r"#EXTINF:\s*([0-9.]+)", text)]
        if extinfs:
            total_dur = sum(extinfs)
            if total_dur > 0:
                return round(total_dur, 2)

        # 2. Master playlist: parse variant stream playlists
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        variant_rel = None

        # Look for the variant URL following #EXT-X-STREAM-INF
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    candidate_line = lines[j]
                    if not candidate_line.startswith("#"):
                        variant_rel = candidate_line
                        break
                if variant_rel:
                    break

        # Fallback: find any line ending with .m3u8 or containing .m3u8 that is not a comment
        if not variant_rel:
            for line in lines:
                if not line.startswith("#") and ".m3u8" in line:
                    variant_rel = line
                    break

        if not variant_rel:
            return None

        # Resolve relative or absolute variant URL
        variant_url = urljoin(playlist_url, variant_rel)
        resp_var = requests.get(variant_url, headers=headers, timeout=timeout)
        if resp_var.status_code != 200 or not resp_var.text:
            return None

        v_extinfs = [float(x) for x in re.findall(r"#EXTINF:\s*([0-9.]+)", resp_var.text)]
        if v_extinfs:
            total_dur = sum(v_extinfs)
            if total_dur > 0:
                return round(total_dur, 2)

    except Exception as e:
        logger.debug(f"HTTP HLS duration probe failed for {playlist_url}: {e}")

    return None


def probe_remote_duration(stream_url: str, timeout: int = 5) -> Optional[float]:
    """
    Fast remote duration probe:
    1. Local file -> probe_media_file
    2. Reddit / HLS stream URL -> fetch HLS playlist via HTTP and sum #EXTINF lengths (< 1s)
    3. Fallback -> ffprobe with a short timeout
    """
    if not stream_url:
        return None

    clean_src = stream_url.replace("file:///", "").replace("file://", "")
    if os.path.isfile(clean_src):
        probe = probe_media_file(Path(clean_src))
        dur = probe.get("duration", 0.0)
        return dur if dur > 0 else None

    # For Reddit videos or HLS streams, construct/identify HLS playlist URL
    hls_url = None
    m = re.search(r"https?://v\.redd\.it/([a-zA-Z0-9]+)", stream_url)
    if m:
        hls_url = f"https://v.redd.it/{m.group(1)}/HLSPlaylist.m3u8"
    elif ".m3u8" in stream_url:
        hls_url = stream_url

    if hls_url:
        dur = probe_hls_duration_http(hls_url, timeout=timeout)
        if dur is not None and dur > 0:
            return dur

    # Secondary fallback for non-HLS streams: fast ffprobe with short timeout
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        stream_url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=min(timeout, 5))
        if res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().split("\n")[0]
            val = float(first_line)
            if val > 0:
                return round(val, 2)
    except Exception as e:
        logger.debug(f"ffprobe remote duration probe fallback failed for {stream_url}: {e}")

    return None


def resolve_cookies_file(source: str, config: Dict[str, Any]) -> Optional[str]:
    """Find a valid cookies file path from config, env vars, or standard locations."""
    # 1. Source-specific config (reddit.cookies_file or twitter.cookies_file)
    cand = config.get(source, {}).get("cookies_file")
    if cand:
        return cand

    # 2. General config
    cand = config.get("cookies_file")
    if cand:
        return cand

    # 3. Environment variables
    for env_var in [f"{source.upper()}_COOKIES_PATH", "COOKIES_PATH", "TWITTER_COOKIES_PATH", "REDDIT_COOKIES_PATH"]:
        env_val = os.getenv(env_var)
        if env_val:
            return env_val

    # 4. Standard file locations
    scraper_dir = Path(__file__).parent.parent.resolve()
    project_root = scraper_dir.parent.resolve()
    for std in [scraper_dir / "cookies.txt", project_root / "cookies.txt", Path("cookies.txt")]:
        if std.is_file():
            return str(std.resolve())

    return None


def download_reddit_with_ffmpeg(
    candidate,
    output_temp_path: Path,
    timeout: int = 45,
) -> bool:
    """
    Directly download and merge Reddit video + audio via FFmpeg without yt-dlp or login credentials.
    Uses HLSPlaylist.m3u8, DASHPlaylist.mpd, or merges fallback_url + DASH audio streams.
    """
    dest_mp4 = output_temp_path.with_suffix(".mp4")

    v_url = candidate.video_url or ""
    fallback_url = (getattr(candidate, "extra", None) or {}).get("fallback_url") or ""
    post_url = candidate.post_url or ""

    base_v = None
    for cand_url in [v_url, fallback_url, post_url]:
        m = re.search(r"(https?://v\.redd\.it/[a-zA-Z0-9]+)", cand_url)
        if m:
            base_v = m.group(1)
            break

    if not base_v:
        return False

    # 1. Direct HLS playlist download & stream copy (muxes video + audio natively)
    hls_url = f"{base_v}/HLSPlaylist.m3u8"
    cmd_hls = ["ffmpeg", "-y", "-v", "warning", "-i", hls_url, "-c", "copy", str(dest_mp4)]
    try:
        res = subprocess.run(cmd_hls, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and dest_mp4.is_file() and dest_mp4.stat().st_size > 1000:
            logger.info(f"Successfully downloaded Reddit video via direct FFmpeg HLS: {hls_url}")
            return True
    except Exception as e:
        logger.debug(f"Direct FFmpeg HLS download failed for {hls_url}: {e}")

    # 2. Direct DASH manifest download
    dash_url = f"{base_v}/DASHPlaylist.mpd"
    cmd_dash = ["ffmpeg", "-y", "-v", "warning", "-i", dash_url, "-c", "copy", str(dest_mp4)]
    try:
        res = subprocess.run(cmd_dash, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and dest_mp4.is_file() and dest_mp4.stat().st_size > 1000:
            logger.info(f"Successfully downloaded Reddit video via direct FFmpeg DASH: {dash_url}")
            return True
    except Exception as e:
        logger.debug(f"Direct FFmpeg DASH download failed for {dash_url}: {e}")

    # 3. If fallback_url is available, attempt to merge with standard DASH audio endpoints
    if fallback_url:
        for audio_ep in ["DASH_AUDIO_128.mp4", "DASH_audio.mp4", "DASH_AUDIO_64.mp4", "audio"]:
            audio_url = f"{base_v}/{audio_ep}"
            cmd_merge = [
                "ffmpeg", "-y", "-v", "warning",
                "-i", fallback_url,
                "-i", audio_url,
                "-c:v", "copy",
                "-c:a", "aac",
                str(dest_mp4)
            ]
            try:
                res = subprocess.run(cmd_merge, capture_output=True, text=True, timeout=timeout)
                if res.returncode == 0 and dest_mp4.is_file() and dest_mp4.stat().st_size > 1000:
                    logger.info(f"Successfully merged Reddit fallback_url + {audio_ep} via FFmpeg")
                    return True
            except Exception as e:
                logger.debug(f"FFmpeg merge fallback failed for {audio_url}: {e}")

        # Fallback: Video-only if audio track does not exist
        cmd_vid = ["ffmpeg", "-y", "-v", "warning", "-i", fallback_url, "-c", "copy", str(dest_mp4)]
        try:
            res = subprocess.run(cmd_vid, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0 and dest_mp4.is_file() and dest_mp4.stat().st_size > 1000:
                logger.info(f"Successfully downloaded Reddit video-only fallback via FFmpeg: {fallback_url}")
                return True
        except Exception:
            pass

    return False


def download_with_ytdlp(
    url: str,
    output_temp_path: Path,
    cookies_file: Optional[str] = None,
    timeout: int = 45,
) -> bool:
    """
    Download video via yt-dlp, automatically merging audio & video streams with FFmpeg.
    Downloads to a temporary file for atomic safety.
    Also supports local file paths for testing.
    """
    # Support local test video paths
    clean_src = url.replace("file:///", "").replace("file://", "")
    if os.path.isfile(clean_src):
        final_mp4 = output_temp_path.with_suffix(".mp4")
        shutil.copy2(clean_src, final_mp4)
        return True

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(output_temp_path.with_suffix("")),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        "retries": 2,
    }

    if cookies_file:
        cookies_path = Path(cookies_file)
        if not cookies_path.is_file():
            scraper_dir = Path(__file__).parent.parent.resolve()
            project_root = scraper_dir.parent.resolve()
            for cand in [scraper_dir / cookies_file, project_root / cookies_file]:
                if cand.is_file():
                    cookies_path = cand
                    break
        if cookies_path.is_file():
            ydl_opts["cookiefile"] = str(cookies_path.resolve())

    # Build list of URLs to attempt:
    urls_to_try = [url]
    if "v.redd.it" in url and not url.endswith((".m3u8", ".mpd", ".mp4")):
        base_v = url.split("?")[0].rstrip("/")
        # Prioritize HLS playlist which streams audio + video directly and bypasses Reddit login gates
        urls_to_try = [f"{base_v}/HLSPlaylist.m3u8", f"{base_v}/DASHPlaylist.mpd", url]

    final_mp4 = output_temp_path.with_suffix(".mp4")
    for target_url in urls_to_try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                retcode = ydl.download([target_url])
                if retcode == 0 and final_mp4.is_file() and final_mp4.stat().st_size > 1000:
                    return True
        except Exception as e:
            logger.debug(f"yt-dlp attempt failed for {target_url}: {e}")

    logger.warning(f"yt-dlp download failed for {url}")
    return False


class VideoDownloader:
    """
    High-level download manager:
      - Downloads to safe temporary location
      - Verifies video duration and resolution
      - Checks binary hash and perceptual frame hash (dedupe)
      - Writes caption.txt and meta.json
      - Updates database
    """

    def __init__(self, db: VideoDatabase, deduplicator: Deduplicator, config: Dict[str, Any]):
        self.db = db
        self.dedup = deduplicator
        self.config = config

    def process_candidate(
        self,
        candidate,
        dest_folder: Path,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Download, validate, deduplicate, and finalize a video candidate.
        Returns:
            (success, message, metadata_or_none)
        """
        dest_folder.mkdir(parents=True, exist_ok=True)
        temp_file = dest_folder / f"temp_{candidate.post_id}.mp4"
        final_video_file = dest_folder / "video.mp4"
        caption_file = dest_folder / "caption.txt"
        meta_file = dest_folder / "meta.json"

        # 0. Duration check before full download (probe remote stream if duration is unknown/None)
        filter_cfg = self.config.get("filters", {})
        min_dur = float(filter_cfg.get("min_duration_seconds", filter_cfg.get("min_duration", 5.0)))
        max_dur = float(filter_cfg.get("max_duration_seconds", filter_cfg.get("max_duration", 90.0)))

        raw_dur = getattr(candidate, "duration", None)
        dur_to_check: Optional[float] = None
        if raw_dur is not None:
            try:
                # Handle int, float or string durations, ignoring MagicMock / uncastable objects in unit tests
                dur_to_check = float(raw_dur)
            except (ValueError, TypeError):
                dur_to_check = None

        was_probed = False
        if dur_to_check is None:
            # RSS or stream without duration in listing metadata: probe stream URL before downloading full file
            probe_target = None
            extra_dict = getattr(candidate, "extra", None) or {}
            cand_urls = [
                getattr(candidate, "video_url", None),
                extra_dict.get("fallback_url") if isinstance(extra_dict, dict) else None,
                getattr(candidate, "post_url", None),
            ]
            for u in cand_urls:
                if u and "v.redd.it" in u:
                    probe_target = u
                    break
            if not probe_target:
                probe_target = candidate.video_url or candidate.post_url

            probed_dur = probe_remote_duration(probe_target)
            if probed_dur is not None:
                candidate.duration = probed_dur
                dur_to_check = probed_dur
                was_probed = True

        if dur_to_check is not None:
            if dur_to_check > 180.0 or dur_to_check > max_dur or dur_to_check < min_dur:
                shutil.rmtree(dest_folder, ignore_errors=True)
                reason_code = "duration_too_short" if dur_to_check < min_dur else "duration_too_long"
                drop_msg = f"{reason_code} ({dur_to_check:.1f}s)" + (" (probed)" if was_probed else "")
                logger.info(f"Dropped before download: [{candidate.source}] '{candidate.title[:40]}' - {drop_msg}")
                self.db.record_seen(
                    post_id=candidate.post_id,
                    source=candidate.source,
                    post_url=candidate.post_url,
                    title=candidate.title,
                    uploader=candidate.uploader,
                    status="filtered",
                    filter_reason=drop_msg,
                )
                return False, f"Dropped before download: {drop_msg}", None

        # 1. Download media to temp file
        cookies = resolve_cookies_file(candidate.source, self.config)

        # For Reddit candidates, attempt direct FFmpeg download first (no login or cookies needed)
        success = False
        if getattr(candidate, "source", "") == "reddit":
            success = download_reddit_with_ffmpeg(candidate, temp_file)

        # Fallback to yt-dlp (with cookies.txt if available)
        if not success or not temp_file.is_file():
            download_url = candidate.video_url or candidate.post_url
            success = download_with_ytdlp(download_url, temp_file, cookies_file=cookies)

        # Secondary retry: If yt-dlp failed, check if we can retry with Reddit HLS playlist directly
        if not success and getattr(candidate, "extra", None):
            fallback = candidate.extra.get("fallback_url") or ""
            v_match = re.search(r"https?://v\.redd\.it/([a-zA-Z0-9]+)", fallback)
            if v_match:
                hls_url = f"https://v.redd.it/{v_match.group(1)}/HLSPlaylist.m3u8"
                logger.info(f"Retrying download via direct Reddit HLS playlist: {hls_url}")
                success = download_with_ytdlp(hls_url, temp_file, cookies_file=cookies)

        if not success or not temp_file.is_file():
            # Clean up temp and remove empty/incomplete destination folder
            if temp_file.is_file():
                temp_file.unlink(missing_ok=True)
            shutil.rmtree(dest_folder, ignore_errors=True)
            self.db.record_seen(
                post_id=candidate.post_id,
                source=candidate.source,
                post_url=candidate.post_url,
                title=candidate.title,
                uploader=candidate.uploader,
                status="failed",
                filter_reason="yt-dlp download failed",
            )
            return False, "Download failed", None

        # 2. Probe media quality (duration, resolution)
        probe = probe_media_file(temp_file)
        dur = probe["duration"]
        w = probe["width"]
        h = probe["height"]

        # 2b. Require an audio track. The uploader rejects silent videos ("Video is missing an
        # audio track"), and Reddit video-only fallbacks produce them. Drop them here so the
        # pipeline uses a spare candidate instead of failing a slot at upload time.
        if self.config.get("filters", {}).get("require_audio", True) and not probe.get("has_audio", True):
            temp_file.unlink(missing_ok=True)
            shutil.rmtree(dest_folder, ignore_errors=True)
            self.db.record_seen(
                post_id=candidate.post_id,
                source=candidate.source,
                post_url=candidate.post_url,
                title=candidate.title,
                uploader=candidate.uploader,
                status="filtered",
                filter_reason="no_audio_track",
            )
            logger.info(f"Dropped after download: [{candidate.source}] '{candidate.title[:40]}' - no_audio_track")
            return False, "Quality check failed: no_audio_track", None

        passed_quality, quality_reason = check_media_post_download(dur, w, h, self.config)
        if not passed_quality:
            temp_file.unlink(missing_ok=True)
            shutil.rmtree(dest_folder, ignore_errors=True)
            self.db.record_seen(
                post_id=candidate.post_id,
                source=candidate.source,
                post_url=candidate.post_url,
                title=candidate.title,
                uploader=candidate.uploader,
                status="filtered",
                filter_reason=quality_reason,
            )
            return False, f"Quality check failed: {quality_reason}", None

        # 3. Media Deduplication (File hash + Perceptual frame hash)
        sample_pcts = self.config.get("deduplication", {}).get(
            "sample_frame_percentages", [15, 50, 85]
        )
        is_dup, dup_reason, file_hash, phashes = self.dedup.check_downloaded_media(
            temp_file, sample_percentages=sample_pcts
        )

        if is_dup:
            temp_file.unlink(missing_ok=True)
            shutil.rmtree(dest_folder, ignore_errors=True)
            self.db.record_seen(
                post_id=candidate.post_id,
                source=candidate.source,
                post_url=candidate.post_url,
                title=candidate.title,
                uploader=candidate.uploader,
                status="duplicate",
                filter_reason=dup_reason,
            )
            return False, f"Duplicate detected: {dup_reason}", None

        # 4. Atomic move to final video.mp4
        shutil.move(str(temp_file), str(final_video_file))

        # 5. Generate and write caption.txt
        caption = llm_generate_caption(candidate.title, {"source": candidate.source})
        if not caption:
            max_words = self.config.get("captions", {}).get("max_words", 8)
            caption = clean_caption(candidate.title, max_words=max_words)

        caption_file.write_text(caption, encoding="utf-8")

        # 6. Write rich meta.json (includes uploader, original URL, and source tracking)
        extra = getattr(candidate, "extra", {}) or {}
        meta = {
            "source": candidate.source,
            "source_type": extra.get("source_type", candidate.source),
            "source_name": extra.get("source_name", extra.get("subreddit", candidate.source)),
            "listing_mode": extra.get("listing_mode", extra.get("listing", "hot")),
            "post_id": candidate.post_id,
            "post_url": candidate.post_url,
            "uploader": candidate.uploader,
            "original_title": candidate.title,
            "caption": caption,
            "score": candidate.score,
            "duration": dur,
            "resolution": f"{w}x{h}",
            "file_hash": file_hash,
            "phashes": phashes,
            "scraped_at": probe.get("scraped_at") or dest_folder.parent.name,
            "video_path": str(final_video_file.resolve()),
            "caption_path": str(caption_file.resolve()),
            "extra": extra,
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # 7. Record downloaded video in database
        self.db.save_downloaded_video(
            source=candidate.source,
            post_id=candidate.post_id,
            post_url=candidate.post_url,
            title=candidate.title,
            uploader=candidate.uploader,
            file_path=str(final_video_file.resolve()),
            caption=caption,
            duration=dur,
            resolution=f"{w}x{h}",
            file_hash=file_hash,
            phashes=phashes,
            metadata=meta,
        )

        return True, "Successfully saved", meta

