import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from loguru import logger
import imagehash


class VideoDatabase:
    """
    SQLite persistent storage for tracking video history and deduplication.
    Ensures no video is ever repeated across days or platforms.
    """

    def __init__(self, db_path: str = "data/videos.db"):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=20.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    post_id TEXT NOT NULL UNIQUE,
                    post_url TEXT,
                    title TEXT,
                    uploader TEXT,
                    scraped_at TEXT NOT NULL,
                    file_path TEXT,
                    caption TEXT,
                    duration REAL,
                    resolution TEXT,
                    file_hash TEXT,
                    phash_1 TEXT,
                    phash_2 TEXT,
                    phash_3 TEXT,
                    status TEXT NOT NULL,
                    filter_reason TEXT,
                    metadata_json TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_post_id ON videos(post_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_post_url ON videos(post_url);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON videos(file_hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON videos(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scraped_at ON videos(scraped_at);")

            # Phase 1 & 2: Circuit breaker, request logs, and subreddit rotation tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reddit_breaker (
                    layer TEXT PRIMARY KEY,
                    blocked_until TEXT,
                    consecutive_failures INTEGER DEFAULT 0,
                    last_status TEXT,
                    updated_at TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    host TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status_code INTEGER,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_req_log_day_host ON request_log(day, host);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subreddit_history (
                    name TEXT PRIMARY KEY,
                    last_scraped_at TEXT,
                    scraped_count INTEGER DEFAULT 0,
                    yield_rate REAL DEFAULT 0.0,
                    approved_count INTEGER DEFAULT 0
                );
            """)

    def is_id_seen(self, post_id: str) -> bool:
        """Check if post_id exists in database."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM videos WHERE post_id = ? LIMIT 1;", (post_id,))
            return cur.fetchone() is not None

    def is_url_seen(self, post_url: str) -> bool:
        """Check if post_url exists in database."""
        if not post_url:
            return False
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM videos WHERE post_url = ? LIMIT 1;", (post_url,))
            return cur.fetchone() is not None

    def is_file_hash_seen(self, file_hash: str) -> bool:
        """Check if identical binary file has already been downloaded."""
        if not file_hash:
            return False
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM videos WHERE file_hash = ? AND status = 'downloaded' LIMIT 1;",
                (file_hash,)
            )
            return cur.fetchone() is not None

    def find_phash_duplicates(
        self,
        candidate_phashes: List[str],
        threshold: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """
        Compare perceptual frame hashes against all existing downloaded videos.
        Returns the matching video record if Hamming distance <= threshold on at least 2 frames.
        """
        if not candidate_phashes or len(candidate_phashes) < 2:
            return None

        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT id, post_id, title, phash_1, phash_2, phash_3
                FROM videos
                WHERE status = 'downloaded'
                  AND phash_1 IS NOT NULL
                  AND phash_2 IS NOT NULL;
            """)
            rows = cur.fetchall()

        cand_hashes = []
        for h_str in candidate_phashes:
            try:
                cand_hashes.append(imagehash.hex_to_hash(h_str))
            except Exception:
                continue

        if not cand_hashes:
            return None

        for row in rows:
            matches = 0
            db_hashes = []
            for col in ("phash_1", "phash_2", "phash_3"):
                val = row[col]
                if val:
                    try:
                        db_hashes.append(imagehash.hex_to_hash(val))
                    except Exception:
                        pass

            # Compare pairs of frame hashes
            num_compare = min(len(cand_hashes), len(db_hashes))
            for i in range(num_compare):
                dist = cand_hashes[i] - db_hashes[i]
                if dist <= threshold:
                    matches += 1

            # If at least 2 keyframes match closely, it's a perceptual duplicate
            if matches >= 2 or (num_compare == 1 and matches == 1):
                return dict(row)

        return None

    def record_seen(
        self,
        post_id: str,
        source: str,
        post_url: str = "",
        title: str = "",
        uploader: str = "",
        status: str = "seen",
        filter_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a candidate as seen or filtered without full media download."""
        now = datetime.now().isoformat()
        meta_str = json.dumps(metadata) if metadata else None

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO videos (
                    source, post_id, post_url, title, uploader,
                    scraped_at, status, filter_reason, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    status = excluded.status,
                    filter_reason = excluded.filter_reason,
                    metadata_json = excluded.metadata_json;
            """, (
                source, post_id, post_url, title, uploader,
                now, status, filter_reason, meta_str
            ))

    def save_downloaded_video(
        self,
        source: str,
        post_id: str,
        post_url: str,
        title: str,
        uploader: str,
        file_path: str,
        caption: str,
        duration: float,
        resolution: str,
        file_hash: str,
        phashes: List[str],
        metadata: Dict[str, Any],
    ) -> None:
        """Save a fully validated and downloaded video record."""
        now = datetime.now().isoformat()
        ph1 = phashes[0] if len(phashes) > 0 else None
        ph2 = phashes[1] if len(phashes) > 1 else None
        ph3 = phashes[2] if len(phashes) > 2 else None
        meta_str = json.dumps(metadata)

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO videos (
                    source, post_id, post_url, title, uploader,
                    scraped_at, file_path, caption, duration, resolution,
                    file_hash, phash_1, phash_2, phash_3, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded', ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    caption = excluded.caption,
                    duration = excluded.duration,
                    resolution = excluded.resolution,
                    file_hash = excluded.file_hash,
                    phash_1 = excluded.phash_1,
                    phash_2 = excluded.phash_2,
                    phash_3 = excluded.phash_3,
                    status = 'downloaded',
                    metadata_json = excluded.metadata_json;
            """, (
                source, post_id, post_url, title, uploader,
                now, file_path, caption, duration, resolution,
                file_hash, ph1, ph2, ph3, meta_str
            ))

    def get_videos_for_date(self, date_str: str) -> List[Dict[str, Any]]:
        """
        Retrieve all downloaded videos for a given date prefix (e.g. '2026-11-12').
        Returns list of ready videos with metadata, caption, and file paths.
        """
        prefix = f"{date_str}%"
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT * FROM videos
                WHERE status = 'downloaded' AND scraped_at LIKE ?
                ORDER BY id ASC;
            """, (prefix,))
            rows = cur.fetchall()

        results = []
        for row in rows:
            d = dict(row)
            if d.get("metadata_json"):
                try:
                    d["meta"] = json.loads(d["metadata_json"])
                except Exception:
                    d["meta"] = {}
            results.append(d)
        return results

    def get_summary_stats(self) -> Dict[str, int]:
        """Return counts of videos by status."""
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT status, COUNT(*) as count FROM videos GROUP BY status;
            """)
            return {row["status"]: row["count"] for row in cur.fetchall()}

    # --- Circuit Breaker & Request Budgeting ---

    def get_requests_count_today(self, day_str: str, host: str = "reddit") -> int:
        """Count total HTTP requests logged for a host on a given calendar day."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE day = ? AND host = ?;",
                (day_str, host),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def log_request(self, day_str: str, host: str, kind: str, status_code: Optional[int] = None) -> None:
        """Record an outbound HTTP request for budget enforcement."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO request_log (day, host, kind, status_code, created_at) VALUES (?, ?, ?, ?, ?);",
                (day_str, host, kind, status_code, now),
            )

    def get_breaker(self, layer: str) -> Optional[Dict[str, Any]]:
        """Retrieve circuit breaker status for a specific layer."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM reddit_breaker WHERE layer = ?;",
                (layer,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_all_breakers(self) -> Dict[str, Dict[str, Any]]:
        """Retrieve circuit breaker status for all layers."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM reddit_breaker;")
            return {row["layer"]: dict(row) for row in cur.fetchall()}

    def update_breaker(
        self,
        layer: str,
        blocked_until: Optional[str],
        consecutive_failures: int,
        last_status: str,
    ) -> None:
        """Update or insert circuit breaker state for a layer."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO reddit_breaker (layer, blocked_until, consecutive_failures, last_status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(layer) DO UPDATE SET
                    blocked_until = excluded.blocked_until,
                    consecutive_failures = excluded.consecutive_failures,
                    last_status = excluded.last_status,
                    updated_at = excluded.updated_at;
            """, (layer, blocked_until, consecutive_failures, last_status, now))

    def reset_breaker(self, layer: str) -> None:
        """Reset circuit breaker for a layer on successful request."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO reddit_breaker (layer, blocked_until, consecutive_failures, last_status, updated_at)
                VALUES (?, NULL, 0, 'ok', ?)
                ON CONFLICT(layer) DO UPDATE SET
                    blocked_until = NULL,
                    consecutive_failures = 0,
                    last_status = 'ok',
                    updated_at = excluded.updated_at;
            """, (layer, now))

    # --- Subreddit Rotation & Yield Tracking ---

    def get_subreddit_history(self) -> Dict[str, Dict[str, Any]]:
        """Retrieve rotation and scrape history for all subreddits."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM subreddit_history;")
            return {row["name"]: dict(row) for row in cur.fetchall()}

    def update_subreddit_scraped(
        self,
        name: str,
        scraped_at: str,
        yield_rate: Optional[float] = None,
        approved_count: Optional[int] = None,
    ) -> None:
        """Record timestamp and metrics when a subreddit is scraped."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM subreddit_history WHERE name = ?;", (name,))
            row = cur.fetchone()
            if row:
                count = row["scraped_count"] + 1
                yr = yield_rate if yield_rate is not None else row["yield_rate"]
                ac = approved_count if approved_count is not None else row["approved_count"]
                conn.execute("""
                    UPDATE subreddit_history
                    SET last_scraped_at = ?, scraped_count = ?, yield_rate = ?, approved_count = ?
                    WHERE name = ?;
                """, (scraped_at, count, yr, ac, name))
            else:
                yr = yield_rate or 0.0
                ac = approved_count or 0
                conn.execute("""
                    INSERT INTO subreddit_history (name, last_scraped_at, scraped_count, yield_rate, approved_count)
                    VALUES (?, ?, 1, ?, ?);
                """, (name, scraped_at, yr, ac))
