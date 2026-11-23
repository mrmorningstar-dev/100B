import os
import json
import sqlite3
import time
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger


DEFAULT_DB_PATH = Path(
    os.environ.get(
        "AGENT_DB_PATH",
        Path(__file__).resolve().parent.parent / "agent" / "data" / "agent.db"
    )
)


@contextmanager
def file_lock(lock_path: Path, timeout: float = 10.0, poll_interval: float = 0.1):
    """Simple cross-platform non-reentrant file lock using atomic O_EXCL file creation."""
    lock_path = Path(lock_path)
    start_time = time.time()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.time() - start_time >= timeout:
                # Check if lock file is stale (> 60s)
                try:
                    mtime = os.path.getmtime(lock_path)
                    if time.time() - mtime > 60:
                        os.unlink(lock_path)
                        continue
                except Exception:
                    pass
                raise TimeoutError(f"Could not acquire file lock on {lock_path} within {timeout}s")
            time.sleep(poll_interval)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                if lock_path.exists():
                    os.unlink(lock_path)
            except Exception:
                pass


def atomic_write_json(file_path: Path, data: Dict[str, Any], indent: int = 2):
    """
    Write JSON file atomically using a temp file and atomic rename, guarded by a file lock.
    """
    file_path = Path(file_path).resolve()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = file_path.with_suffix(file_path.suffix + ".lock")
    temp_file = file_path.with_name(f"{file_path.name}.tmp.{os.getpid()}")

    with file_lock(lock_file):
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, file_path)


class UploadDatabase:
    """
    Direct SQLite database interface for Phase 4 uploader.
    Uses plain sqlite3; completely decoupled from agent.core.
    """

    def __init__(self, db_path: Optional[Path] = None):
        target = Path(db_path or os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH).resolve()

        # Hard safety guard: under pytest or testing, NEVER touch the real agent/data/agent.db
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("PYTEST_RUNNING")
            or "pytest" in sys.modules
            or os.environ.get("TESTING")
        )
        prod_db = (Path(__file__).resolve().parent.parent / "agent" / "data" / "agent.db").resolve()
        if is_testing and target == prod_db:
            alt = os.environ.get("AGENT_DB_PATH")
            if alt and Path(alt).resolve() != prod_db:
                target = Path(alt).resolve()
            else:
                import tempfile
                target = (Path(tempfile.gettempdir()) / "pytest_agent_test.db").resolve()
                logger.warning(f"TEST SAFETY GUARD: Redirected UploadDatabase from production {prod_db} to temporary {target}")

        self.db_path = target
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create uploads and daily_quota tables if they do not already exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS uploads (
                    post_id TEXT PRIMARY KEY,
                    target_date TEXT NOT NULL,
                    slot_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    video_id TEXT,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    uploaded_at TEXT,
                    privacy TEXT,
                    publish_at TEXT,
                    pacific_date TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_date ON uploads(target_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);")

            # Migration for existing databases without uploaded_at / pacific_date columns
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(uploads)").fetchall()]
                if "uploaded_at" not in cols:
                    conn.execute("ALTER TABLE uploads ADD COLUMN uploaded_at TEXT;")
                if "attempts" not in cols:
                    conn.execute("ALTER TABLE uploads ADD COLUMN attempts INTEGER DEFAULT 0;")
                if "pacific_date" not in cols:
                    conn.execute("ALTER TABLE uploads ADD COLUMN pacific_date TEXT;")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_pacific_date ON uploads(pacific_date);")

            # Persistent daily quota table tracked by Pacific-time day
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_quota (
                    pacific_date TEXT PRIMARY KEY,
                    inserts_count INTEGER DEFAULT 0,
                    reads_count INTEGER DEFAULT 0,
                    quota_used INTEGER DEFAULT 0,
                    updated_at TEXT
                );
            """)

            # Daily Master Pipeline Runs (Phase 5 Dashboard Instrumentation)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    uploaded INTEGER DEFAULT 0,
                    scheduled INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    message TEXT,
                    next_attempt_at TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_runs_status ON daily_runs(status);")

            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_runs)").fetchall()]
                if "next_attempt_at" not in cols:
                    conn.execute("ALTER TABLE daily_runs ADD COLUMN next_attempt_at TEXT;")
            except sqlite3.OperationalError:
                pass

            # Backfill existing uploaded rows missing pacific_date
            rows = conn.execute("SELECT post_id, uploaded_at FROM uploads WHERE pacific_date IS NULL AND uploaded_at IS NOT NULL;").fetchall()
            for r in rows:
                try:
                    dt = datetime.fromisoformat(r["uploaded_at"])
                    from zoneinfo import ZoneInfo
                    p_str = dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                    conn.execute("UPDATE uploads SET pacific_date = ? WHERE post_id = ?;", (p_str, r["post_id"]))
                except Exception:
                    pass
            conn.commit()

    @staticmethod
    def get_pacific_date() -> str:
        """Return current date formatted as YYYY-MM-DD in America/Los_Angeles (Pacific Time)."""
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

    def get_daily_quota(self, pacific_date: str) -> Dict[str, int]:
        """Fetch persistent quota usage for a given Pacific-time date."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT inserts_count, reads_count, quota_used FROM daily_quota WHERE pacific_date = ?",
                (pacific_date,)
            ).fetchone()
            if row:
                return {
                    "inserts_count": int(row["inserts_count"]),
                    "reads_count": int(row["reads_count"]),
                    "quota_used": int(row["quota_used"]),
                }
            return {"inserts_count": 0, "reads_count": 0, "quota_used": 0}

    def record_quota_usage(
        self,
        pacific_date: str,
        insert_delta: int = 0,
        read_delta: int = 0,
        cost_delta: int = 0,
    ):
        """Atomically record quota usage into daily_quota table."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO daily_quota (pacific_date, inserts_count, reads_count, quota_used, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pacific_date) DO UPDATE SET
                    inserts_count = daily_quota.inserts_count + excluded.inserts_count,
                    reads_count = daily_quota.reads_count + excluded.reads_count,
                    quota_used = daily_quota.quota_used + excluded.quota_used,
                    updated_at = excluded.updated_at;
            """, (pacific_date, insert_delta, read_delta, cost_delta, now))
            conn.commit()

    def get_pacific_uploads_count(self, pacific_date: str) -> int:
        """Count how many videos were successfully uploaded/scheduled on a specific Pacific-time date."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM uploads WHERE pacific_date = ? AND status IN ('uploaded', 'scheduled')",
                (pacific_date,)
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_upload(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Fetch upload record for a post_id."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE post_id = ?", (post_id,)).fetchone()
            return dict(row) if row else None

    def record_attempt(
        self,
        post_id: str,
        target_date: str,
        slot_time: str,
        status: str,
        video_id: Optional[str] = None,
        privacy: Optional[str] = None,
        publish_at: Optional[str] = None,
        error: Optional[str] = None,
        pacific_date: Optional[str] = None,
    ):
        """Insert or update upload record when an attempt begins or finishes."""
        now = datetime.now().isoformat()
        p_date = pacific_date or self.get_pacific_date()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO uploads (
                    post_id, target_date, slot_time, status, video_id, attempts, last_error, uploaded_at, privacy, publish_at, pacific_date
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    status = excluded.status,
                    video_id = COALESCE(excluded.video_id, uploads.video_id),
                    attempts = uploads.attempts + 1,
                    last_error = excluded.last_error,
                    uploaded_at = COALESCE(excluded.uploaded_at, uploads.uploaded_at),
                    privacy = COALESCE(excluded.privacy, uploads.privacy),
                    publish_at = COALESCE(excluded.publish_at, uploads.publish_at),
                    pacific_date = COALESCE(excluded.pacific_date, uploads.pacific_date);
            """, (post_id, target_date, slot_time, status, video_id, error, now if status in ("uploaded", "scheduled") else None, privacy, publish_at, p_date if status in ("uploaded", "scheduled") else None))
            conn.commit()

    def update_status(
        self,
        post_id: str,
        status: str,
        video_id: Optional[str] = None,
        error: Optional[str] = None,
        privacy: Optional[str] = None,
        pacific_date: Optional[str] = None,
    ):
        """Update status and metadata of an existing upload."""
        now = datetime.now().isoformat()
        p_date = pacific_date or self.get_pacific_date()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE uploads
                SET status = ?,
                    video_id = COALESCE(?, video_id),
                    last_error = ?,
                    uploaded_at = CASE WHEN ? IN ('uploaded', 'scheduled') THEN ? ELSE uploaded_at END,
                    privacy = COALESCE(?, privacy),
                    pacific_date = CASE WHEN ? IN ('uploaded', 'scheduled') THEN ? ELSE pacific_date END
                WHERE post_id = ?;
            """, (status, video_id, error, status, now, privacy, status, p_date, post_id))
            conn.commit()

    def get_uploads_by_date(self, target_date: str) -> List[Dict[str, Any]]:
        """Return all uploads for a given target_date."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM uploads WHERE target_date = ? ORDER BY slot_time ASC", (target_date,)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_uploads(self, days: int = 7) -> List[Dict[str, Any]]:
        """Return uploads from the last N days."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM uploads WHERE target_date >= ? ORDER BY target_date DESC, slot_time ASC", (cutoff,)).fetchall()
            return [dict(r) for r in rows]

    def get_daily_upload_count(self, target_date: str) -> int:
        """Count how many videos were uploaded/scheduled for a target_date."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM uploads WHERE target_date = ? AND status IN ('uploaded', 'scheduled')",
                (target_date,)
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_available_reserve_video_paths(self) -> set:
        """
        Query reserve_pool table for paths of all active/available reserve clips.
        Returns a set of resolved Path objects that must never be purged.
        """
        with self._get_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT video_path FROM reserve_pool WHERE status = 'available'"
                ).fetchall()
                paths = set()
                for r in rows:
                    p = r["video_path"]
                    if p:
                        try:
                            paths.add(Path(p).resolve())
                        except Exception:
                            pass
                return paths
            except sqlite3.OperationalError:
                return set()

    def record_daily_run_start(self) -> int:
        """Record the beginning of a daily pipeline execution."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO daily_runs (started_at, status)
                VALUES (?, 'running')
            """, (now,))
            conn.commit()
            return cursor.lastrowid

    def record_daily_run_finish(
        self,
        run_id: int,
        status: str,
        uploaded: int = 0,
        scheduled: int = 0,
        failed: int = 0,
        message: str = "",
        next_attempt_at: Optional[str] = None,
    ) -> None:
        """Update daily_runs upon run completion."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_runs
                SET finished_at = ?, status = ?, uploaded = ?, scheduled = ?, failed = ?, message = ?, next_attempt_at = ?
                WHERE id = ?
            """, (now, status, uploaded, scheduled, failed, message, next_attempt_at, run_id))
            conn.commit()

    def get_last_daily_run(self) -> Optional[Dict[str, Any]]:
        """Retrieve most recent daily_runs record."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM daily_runs
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            return dict(row) if row else None


def update_schedule_slot(schedule_path: Path, post_id: str, updates: Dict[str, Any]):
    """
    Atomically update fields for a specific slot in schedule.json.
    """
    schedule_path = Path(schedule_path).resolve()
    if not schedule_path.is_file():
        logger.warning(f"Cannot update schedule slot: {schedule_path} does not exist.")
        return

    with open(schedule_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    slots = data.get("slots", [])
    found = False
    for slot in slots:
        if slot.get("post_id") == post_id:
            slot.update(updates)
            found = True
            break

    if found:
        atomic_write_json(schedule_path, data)
        logger.debug(f"Updated slot for {post_id} in {schedule_path.name}")
    else:
        logger.warning(f"Slot with post_id {post_id} not found in {schedule_path.name}")
