import os
import sys
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger

import sqlite3
import json

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
SCRAPER_DIR = PROJECT_ROOT / "scraper"


class ScraperTool:
    """
    Wraps Phase 2 Scraper without duplicating any scraping or download logic.
    Executes scraper runs based on the agent's plan or fetches ready clips from data/raw.
    """

    def __init__(self, scraper_dir: Path = SCRAPER_DIR):
        self.scraper_dir = scraper_dir
        self.db_path = scraper_dir / "data" / "videos.db"

    def run_scrape(
        self,
        target_count: int,
        date_str: str,
        sources: List[str],
        subreddits: Optional[List[str]] = None,
        twitter_accounts: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> bool:
        """Execute Phase 2 scraper CLI as a tool."""
        script_path = self.scraper_dir / "scrape.py"
        sources_arg = ",".join(sources) if sources else "reddit"

        cmd = [
            sys.executable,
            str(script_path),
            "--target", str(target_count),
            "--sources", sources_arg,
            "--date", date_str,
        ]
        if subreddits:
            cmd.extend(["--subreddits", ",".join(subreddits)])
        if twitter_accounts:
            cmd.extend(["--twitter-accounts", ",".join(twitter_accounts)])
        if dry_run:
            cmd.append("--dry-run")

        # Set PYTHONUTF8=1 and UTF-8 encoding so emoji output never breaks on Windows
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        logger.info(f"ScraperTool running: {' '.join(cmd)}")
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.scraper_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=600,
            )
            if res.returncode != 0:
                logger.warning(f"Scraper process completed with exit code {res.returncode}:\n{res.stderr[-500:]}")
                if getattr(self, "last_scrape_status", "ok") != "blocked":
                    self.last_scrape_status = "error"
            else:
                logger.info("ScraperTool completed successfully.")
            return res.returncode == 0
        except Exception as e:
            logger.error(f"Error invoking Phase 2 scraper tool: {e}")
            return False

    def list_candidates(
        self,
        target_count: int,
        date_str: str,
        sources: List[str],
        subreddits: Optional[List[str]] = None,
        custom_sources: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Run scraper in --list-only mode and return candidate dictionaries."""
        script_path = self.scraper_dir / "scrape.py"
        candidates_out = self.scraper_dir / "data" / "candidates" / f"{date_str}_candidates.json"
        candidates_out.parent.mkdir(parents=True, exist_ok=True)

        sources_arg = ",".join(sources) if sources else "reddit"
        cmd = [
            sys.executable,
            str(script_path),
            "--list-only",
            "--target", str(target_count),
            "--sources", sources_arg,
            "--date", date_str,
            "--candidates-output", str(candidates_out),
        ]
        if subreddits:
            cmd.extend(["--subreddits", ",".join(subreddits)])
        if custom_sources:
            cmd.extend(["--sources-json", json.dumps(custom_sources)])

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        logger.info(f"ScraperTool listing candidates: {' '.join(cmd[:6])}...")
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.scraper_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=300,
            )
            if res.returncode != 0:
                logger.warning(f"Scraper --list-only returned code {res.returncode}: {res.stderr[-300:]}")
            if res.stdout:
                for line in res.stdout.splitlines():
                    if "dropped before download:" in line:
                        logger.info(line.strip())

            # Read structured scrape telemetry if generated
            result_path = self.scraper_dir / "data" / "candidates" / f"{date_str}_scrape_result.json"
            if result_path.is_file():
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        self.last_scrape_result = json.load(f)
                        self.last_scrape_status = self.last_scrape_result.get("status", "ok")
                except Exception:
                    self.last_scrape_result = {}
                    self.last_scrape_status = "ok"
            else:
                self.last_scrape_result = {}
                self.last_scrape_status = "ok"

            # A non-zero exit code that isn't "blocked" is an unhandled failure/crash
            if res.returncode != 0 and self.last_scrape_status != "blocked":
                self.last_scrape_status = "error"
                logger.error(f"Scraper process crashed with code {res.returncode}. Recorded last_scrape_status='error'.")

            if candidates_out.is_file():
                with open(candidates_out, "r", encoding="utf-8") as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"Error listing candidates: {e}")
            self.last_scrape_status = "error"
            return []

    def is_scraper_blocked(self) -> bool:
        """Check whether the most recent scrape operation reported a circuit breaker block."""
        return getattr(self, "last_scrape_status", "ok") == "blocked"

    def has_scraper_error(self) -> bool:
        """Check whether the most recent scrape operation crashed or exited non-zero without being blocked."""
        return getattr(self, "last_scrape_status", "ok") == "error"

    def download_candidates(
        self,
        candidates: List[Dict[str, Any]],
        target_count: int,
        date_str: str,
        workers: int = 4,
    ) -> bool:
        """Download only surviving candidates that passed title pre-screening."""
        if not candidates:
            logger.info("No candidates provided for download.")
            return True

        vetted_path = self.scraper_dir / "data" / "candidates" / f"{date_str}_vetted.json"
        vetted_path.parent.mkdir(parents=True, exist_ok=True)
        with open(vetted_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2)

        script_path = self.scraper_dir / "scrape.py"
        cmd = [
            sys.executable,
            str(script_path),
            "--download-only",
            "--candidates-file", str(vetted_path),
            "--target", str(target_count),
            "--date", date_str,
            "--workers", str(workers),
        ]

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        logger.info(f"ScraperTool downloading {len(candidates)} vetted candidates...")
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.scraper_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=600,
            )
            if res.returncode != 0:
                logger.warning(f"Scraper --download-only code {res.returncode}: {res.stderr[-300:]}")
                if getattr(self, "last_scrape_status", "ok") != "blocked":
                    self.last_scrape_status = "error"
            if res.stdout:
                for line in res.stdout.splitlines():
                    if "dropped before download:" in line or "Successfully Downloaded" in line:
                        logger.info(line.strip())
            return res.returncode == 0
        except Exception as e:
            logger.error(f"Error downloading candidates: {e}")
            return False

    def get_downloaded_videos(self, date_str: str) -> List[Dict[str, Any]]:
        """
        Build candidate video dictionaries exclusively from raw/<date>/*/meta.json,
        which already has all metadata, file hashes, captions, and exact media paths.
        """
        raw_date_dir = self.scraper_dir / "data" / "raw" / date_str
        if not raw_date_dir.is_dir():
            logger.info(f"Raw directory does not exist yet: {raw_date_dir}")
            return []

        candidates = []
        for meta_file in sorted(raw_date_dir.glob("*/meta.json")):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                # Ensure video.mp4 exists in the folder
                vid_file = meta_file.parent / "video.mp4"
                if not vid_file.is_file():
                    logger.warning(f"Skipping {meta_file.parent.name}: video.mp4 missing")
                    continue

                caption_file = meta_file.parent / "caption.txt"

                candidates.append({
                    "post_id": meta.get("post_id") or meta_file.parent.name,
                    "source": meta.get("source", "unknown"),
                    "post_url": meta.get("post_url", ""),
                    "title": meta.get("original_title") or meta.get("title") or meta.get("caption", ""),
                    "original_caption": meta.get("caption", ""),
                    "caption": meta.get("caption", ""),
                    "uploader": meta.get("uploader", "unknown"),
                    "video_path": str(vid_file.resolve()),
                    "caption_path": str(caption_file.resolve()) if caption_file.is_file() else "",
                    "duration": meta.get("duration"),
                    "resolution": meta.get("resolution"),
                    "file_hash": meta.get("file_hash"),
                    "score": meta.get("score", 500),
                    "meta": meta,
                })
            except Exception as e:
                logger.warning(f"Error reading metadata from {meta_file}: {e}")

        return candidates
