import os
import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
from loguru import logger


class AgentDatabase:
    """
    SQLite persistence layer for Phase 3 Curation Agent.
    Manages:
      - LLM judgement cache (keyed by media file hash)
      - Agent decisions and reason logging (for tuning the judge)
      - LLM call logs (tokens, costs, latency)
      - Reserve pool (backfill leftover approved videos across days)
      - Schedule entries (hourly slots for Phase 4 YouTube Uploader)
    """

    def __init__(self, db_path: Optional[Path] = None):
        target = Path(db_path or os.environ.get("AGENT_DB_PATH") or (Path(__file__).resolve().parent.parent / "data" / "agent.db")).resolve()

        # Hard safety guard: under pytest or testing, NEVER touch the real agent/data/agent.db
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("PYTEST_RUNNING")
            or "pytest" in sys.modules
            or os.environ.get("TESTING")
        )
        prod_db = (Path(__file__).resolve().parent.parent / "data" / "agent.db").resolve()
        if is_testing and target == prod_db:
            alt = os.environ.get("AGENT_DB_PATH")
            if alt and Path(alt).resolve() != prod_db:
                target = Path(alt).resolve()
            else:
                import tempfile
                target = (Path(tempfile.gettempdir()) / "pytest_agent_test.db").resolve()
                logger.warning(f"TEST SAFETY GUARD: Redirected AgentDatabase from production {prod_db} to temporary {target}")

        self.db_path = target
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        """Create necessary agent tables if they do not exist."""
        with self._get_connection() as conn:
            # 1. LLM Vision Judge Cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_judge_cache (
                    file_hash TEXT PRIMARY KEY,
                    funny_score REAL NOT NULL,
                    is_english_or_no_speech INTEGER NOT NULL,
                    is_safe_for_ads INTEGER NOT NULL,
                    has_visible_watermark INTEGER NOT NULL,
                    reason TEXT,
                    cached_at TEXT NOT NULL
                );
            """)

            # 2. Agent Decision Logging (Tuning History)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    file_hash TEXT,
                    hybrid_score REAL,
                    status TEXT NOT NULL,
                    decision_reason TEXT,
                    target_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

            # 3. LLM API Usage & Cost Log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    latency_s REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

            # 4. Reserve Pool (Surplus approved clips for backfill)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reserve_pool (
                    post_id TEXT PRIMARY KEY,
                    video_path TEXT NOT NULL,
                    caption TEXT,
                    title TEXT,
                    description TEXT,
                    uploader TEXT,
                    post_url TEXT,
                    score REAL,
                    duration REAL,
                    resolution TEXT,
                    status TEXT DEFAULT 'available',
                    added_at TEXT NOT NULL,
                    used_at TEXT
                );
            """)

            # 5. Daily Schedule Table for Phase 4 (Migrate to UNIQUE(target_date, slot_time))
            sched_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule'").fetchone()
            if sched_row and sched_row[0]:
                sql_str = sched_row[0]
                sql_clean = sql_str.replace(" ", "")
                if "UNIQUE(target_date,slot_hour)" in sql_clean:
                    logger.info("Migrating schedule table constraint from (target_date, slot_hour) to (target_date, slot_time)...")
                    conn.execute("ALTER TABLE schedule RENAME TO schedule_old_migration;")
                    conn.execute("""
                        CREATE TABLE schedule (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            target_date TEXT NOT NULL,
                            slot_hour INTEGER NOT NULL,
                            slot_time TEXT NOT NULL,
                            post_id TEXT NOT NULL,
                            video_path TEXT NOT NULL,
                            title TEXT,
                            description TEXT,
                            caption TEXT,
                            source_url TEXT,
                            uploader TEXT,
                            status TEXT DEFAULT 'queued',
                            created_at TEXT NOT NULL,
                            UNIQUE(target_date, slot_time)
                        );
                    """)
                    conn.execute("""
                        INSERT OR IGNORE INTO schedule (
                            id, target_date, slot_hour, slot_time, post_id, video_path,
                            title, description, caption, source_url, uploader, status, created_at
                        ) SELECT id, target_date, slot_hour, slot_time, post_id, video_path,
                                 title, description, caption, source_url, uploader, status, created_at
                          FROM schedule_old_migration;
                    """)
                    conn.execute("DROP TABLE schedule_old_migration;")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    slot_hour INTEGER NOT NULL,
                    slot_time TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    video_path TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    caption TEXT,
                    source_url TEXT,
                    uploader TEXT,
                    status TEXT DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    UNIQUE(target_date, slot_time)
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_target_date ON schedule(target_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_slot_time ON schedule(target_date, slot_time);")

            # 6. Phase 3.5: Dynamic Source Registry
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    listing_mode TEXT NOT NULL DEFAULT 'hot',
                    status TEXT NOT NULL DEFAULT 'active',
                    found_count INTEGER DEFAULT 0,
                    downloaded_count INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    rejected_count INTEGER DEFAULT 0,
                    yield_rate REAL DEFAULT 0.0,
                    consecutive_empty_runs INTEGER DEFAULT 0,
                    pagination_cursor TEXT DEFAULT NULL,
                    reject_reasons_json TEXT DEFAULT '{}',
                    added_by TEXT DEFAULT 'config',
                    last_used_date TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_type, name, listing_mode)
                );
            """)

            # 7. Phase 3.5: Daily Discovery Logs
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discovery_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    suggested_by_llm_json TEXT NOT NULL,
                    verified_json TEXT NOT NULL,
                    rejected_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 8. Pending Judge Retries (Videos whose judge call failed or timed out)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS judge_retries (
                    post_id TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    video_path TEXT,
                    meta_json TEXT,
                    failure_reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL
                );
            """)

            # 9. Curation Run Stats (for local dashboard)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    found INTEGER DEFAULT 0,
                    dropped_prescreen INTEGER DEFAULT 0,
                    dropped_duration INTEGER DEFAULT 0,
                    downloaded INTEGER DEFAULT 0,
                    dropped_filters INTEGER DEFAULT 0,
                    judged INTEGER DEFAULT 0,
                    rejected_judge INTEGER DEFAULT 0,
                    borderline INTEGER DEFAULT 0,
                    approved INTEGER DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    reserve_added INTEGER DEFAULT 0,
                    reject_reasons_json TEXT DEFAULT '{}',
                    source_stats_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_stats_date ON run_stats(target_date);")

            # 10. Daily Master Pipeline Runs (written by run_daily.py)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    uploaded INTEGER DEFAULT 0,
                    scheduled INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    message TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_runs_status ON daily_runs(status);")

            # 11. Phase 6: Rights Check, Claims, Learned Blocklist & Provider Usage
            self._migrate_rights_tables(conn)

    def _migrate_rights_tables(self, conn: sqlite3.Connection) -> None:
        """Initialize Phase 6 tables, columns, and initial claims seeds."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rights_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                layers_json TEXT NOT NULL,
                risk_score INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rights_checks_post_id ON rights_checks(post_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rights_checks_target_date ON rights_checks(target_date);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT,
                post_id TEXT NOT NULL,
                claim_type TEXT NOT NULL,
                claimant TEXT NOT NULL,
                asset_title TEXT,
                found_in TEXT,
                action TEXT NOT NULL,
                noted_at TEXT NOT NULL,
                source_name TEXT,
                uploader TEXT,
                phashes_json TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_post_id ON claims(post_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_video_id ON claims(video_id);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rights_blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                value TEXT NOT NULL,
                source_claim_id INTEGER REFERENCES claims(id),
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rights_blocklist_type_val ON rights_blocklist(type, value);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                calls INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                UNIQUE(day, provider)
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                post_id TEXT PRIMARY KEY,
                target_date TEXT,
                video_id TEXT,
                slot_time TEXT,
                publish_at TEXT,
                status TEXT DEFAULT 'pending',
                last_error TEXT,
                claim_status TEXT DEFAULT 'none',
                rights_status TEXT DEFAULT 'unreviewed',
                rights_risk INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_target_date ON uploads(target_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_video_id ON uploads(video_id);")

        # Extend uploads table non-destructively
        try:
            uploads_cols = [r[1] for r in conn.execute("PRAGMA table_info(uploads)")]
            if "privacy" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN privacy TEXT DEFAULT 'private'")
            if "rights_status" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN rights_status TEXT DEFAULT 'unreviewed'")
            if "rights_risk" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN rights_risk INTEGER DEFAULT 0")
            if "claim_status" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN claim_status TEXT DEFAULT 'none'")
            if "uploaded_at" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN uploaded_at TEXT")
            if "attempts" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN attempts INTEGER DEFAULT 0")
            if "pacific_date" not in uploads_cols:
                conn.execute("ALTER TABLE uploads ADD COLUMN pacific_date TEXT")
        except sqlite3.OperationalError:
            pass

        # Extend claims table non-destructively (Phase 6.1)
        try:
            claims_cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)")]
            if "state" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN state TEXT DEFAULT 'active'")
            if "source" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN source TEXT DEFAULT 'manual'")
            if "detected_at" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN detected_at TEXT")
            if "last_seen_at" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN last_seen_at TEXT")
            if "released_at" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN released_at TEXT")
            if "message_id" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN message_id TEXT")
            if "track_title" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN track_title TEXT")
            if "track_artist" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN track_artist TEXT")
            if "affects_reach" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN affects_reach INTEGER DEFAULT 0")
            if "action_taken" not in claims_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN action_taken TEXT")
        except sqlite3.OperationalError:
            pass

        # Phase 6.1: claim_emails (idempotent ingestion log)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_emails (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT,
                received_at TEXT NOT NULL,
                subject TEXT,
                sender TEXT,
                event_type TEXT,
                matched_post_id TEXT,
                matched_video_id TEXT,
                status TEXT NOT NULL,
                raw_snippet TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_emails_status ON claim_emails(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_emails_post_id ON claim_emails(matched_post_id);")

        # Phase 6.1: claim_actions (audit log for auto-actions & dry-run simulations)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER,
                post_id TEXT NOT NULL,
                video_id TEXT,
                action_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 1,
                result TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_actions_claim_id ON claim_actions(claim_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_actions_post_id ON claim_actions(post_id);")

        # Extend schedule table non-destructively
        try:
            sched_cols = [r[1] for r in conn.execute("PRAGMA table_info(schedule)")]
            if "rights_status" not in sched_cols:
                conn.execute("ALTER TABLE schedule ADD COLUMN rights_status TEXT DEFAULT 'unreviewed'")
            if "rights_risk" not in sched_cols:
                conn.execute("ALTER TABLE schedule ADD COLUMN rights_risk INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Extend reserve_pool table non-destructively
        try:
            reserve_cols = [r[1] for r in conn.execute("PRAGMA table_info(reserve_pool)")]
            if "rights_status" not in reserve_cols:
                conn.execute("ALTER TABLE reserve_pool ADD COLUMN rights_status TEXT DEFAULT 'unreviewed'")
            if "rights_risk" not in reserve_cols:
                conn.execute("ALTER TABLE reserve_pool ADD COLUMN rights_risk INTEGER DEFAULT 0")
            if "rights_checked_at" not in reserve_cols:
                conn.execute("ALTER TABLE reserve_pool ADD COLUMN rights_checked_at TEXT")
        except sqlite3.OperationalError:
            pass

        # Seed the confirmed claims: car clip kept/pending Studio check, goat clip pulled, delivery driver Fortunate Son
        cur = conn.execute("SELECT COUNT(*) FROM claims WHERE post_id = 'reddit_1xv7kmf'")
        if cur.fetchone()[0] == 0:
            conn.execute("""
                INSERT INTO claims (
                    video_id, post_id, claim_type, claimant, asset_title,
                    found_in, action, noted_at, source_name, uploader, notes,
                    state, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown_pending_confirmation', 'seed')
            """, (
                "xwJJMYnUgSY",
                "reddit_1xv7kmf",
                "visual",
                "CollabDRM",
                "Collab ViralHog - Female Pheasant Not Impressed By Mating Dance",
                "0:02 - 0:19",
                "kept",
                "2026-11-16T18:00:00-05:00",
                "r/funny",
                "KnownUploader",
                "Recorded, near-duplicates blocked, marked possible false match"
            ))
        else:
            conn.execute("UPDATE claims SET action = 'kept', state = 'unknown_pending_confirmation' WHERE post_id = 'reddit_1xv7kmf' AND state = 'active'")

        cur = conn.execute("SELECT COUNT(*) FROM claims WHERE post_id = 'reddit_1xrfauk'")
        if cur.fetchone()[0] == 0:
            conn.execute("""
                INSERT INTO claims (
                    video_id, post_id, claim_type, claimant, asset_title,
                    found_in, action, noted_at, source_name, uploader, notes,
                    state, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'removed', 'seed')
            """, (
                "f38cep_p8p8",
                "reddit_1xrfauk",
                "audio",
                "SME",
                "Eye of the Tiger by Survivor (SME for Volcano)",
                "0:00 - 0:15",
                "pulled",
                "2026-11-16T18:00:00-05:00",
                "r/funny",
                "KnownUploader",
                "Commercial music track identified by audio fingerprint"
            ))
        else:
            conn.execute("UPDATE claims SET action = 'pulled' WHERE post_id = 'reddit_1xrfauk'")

        # Phase 6.1: Seed Fortunate Son claim for reddit_1xw42j0 (Delivery Driver)
        cur = conn.execute("SELECT COUNT(*) FROM claims WHERE post_id = 'reddit_1xw42j0'")
        if cur.fetchone()[0] == 0:
            conn.execute("""
                INSERT INTO claims (
                    video_id, post_id, claim_type, claimant, asset_title,
                    track_title, track_artist, action, state, source, noted_at, detected_at,
                    affects_reach, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'held', 'active', 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, ?)
            """, (
                "HcwZS5Sxv_Y",
                "reddit_1xw42j0",
                "audio",
                "unknown",
                "Fortunate Son",
                "Fortunate Son",
                "Creedence Clearwater Revival",
                "Seeded from YouTube Studio screenshot facts"
            ))

        # Phase 6.1: Deduplicate any manual placeholder claims that have a corresponding confirmed/email claim
        try:
            rows = conn.execute("""
                SELECT c1.id AS placeholder_id, c2.id AS confirmed_id
                FROM claims c1
                JOIN claims c2 ON (
                    (c1.video_id IS NOT NULL AND c1.video_id != '' AND c1.video_id = c2.video_id)
                    OR c1.post_id = c2.post_id
                ) AND c1.id != c2.id
                  AND LOWER(COALESCE(c1.claim_type, '')) = LOWER(COALESCE(c2.claim_type, ''))
                  AND (
                      LOWER(COALESCE(c1.asset_title, '')) = LOWER(COALESCE(c2.asset_title, ''))
                      OR LOWER(COALESCE(c1.track_title, '')) = LOWER(COALESCE(c2.asset_title, ''))
                      OR LOWER(COALESCE(c1.asset_title, '')) = LOWER(COALESCE(c2.track_title, ''))
                      OR LOWER(COALESCE(c1.track_title, '')) = LOWER(COALESCE(c2.track_title, ''))
                  )
                WHERE (LOWER(c1.claimant) IN ('unknown', 'unknown claimant', '') OR c1.source IN ('manual', 'seed'))
                  AND (c2.source = 'email' OR LOWER(c2.claimant) NOT IN ('unknown', 'unknown claimant', ''))
            """).fetchall()

            for r in rows:
                placeholder_id = r["placeholder_id"]
                confirmed_id = r["confirmed_id"]
                conn.execute("""
                    UPDATE claims
                    SET track_artist = COALESCE(claims.track_artist, (SELECT track_artist FROM claims WHERE id = ?)),
                        track_title = COALESCE(claims.track_title, (SELECT track_title FROM claims WHERE id = ?))
                    WHERE id = ?
                """, (placeholder_id, placeholder_id, confirmed_id))
                conn.execute("UPDATE claim_actions SET claim_id = ? WHERE claim_id = ?", (confirmed_id, placeholder_id))
                conn.execute("DELETE FROM claims WHERE id = ?", (placeholder_id,))
        except Exception as e:
            logger.warning(f"Claims deduplication migration skipped: {e}")

        try:
            conn.execute("UPDATE uploads SET claim_status = 'claimed', rights_status = 'claimed', privacy = 'public' WHERE post_id = 'reddit_1xv7kmf'")
            conn.execute("UPDATE uploads SET claim_status = 'claimed', rights_status = 'claimed', status = 'deleted' WHERE post_id = 'reddit_1xrfauk'")
            conn.execute("UPDATE uploads SET claim_status = 'claimed', rights_status = 'claimed' WHERE post_id = 'reddit_1xw42j0'")
            conn.execute("UPDATE schedule SET rights_status = 'claimed' WHERE post_id = 'reddit_1xv7kmf'")
            conn.execute("UPDATE schedule SET status = 'deleted' WHERE post_id = 'reddit_1xrfauk'")
            conn.execute("UPDATE schedule SET rights_status = 'claimed' WHERE post_id = 'reddit_1xw42j0'")
        except sqlite3.OperationalError:
            pass

    # --- LLM Judge Cache ---
    def get_cached_judge(self, file_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached Claude Vision evaluation for a video file hash."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM llm_judge_cache WHERE file_hash = ?", (file_hash,))
            row = cur.fetchone()
            if row:
                return {
                    "funny_score": row["funny_score"],
                    "is_english_or_no_speech": bool(row["is_english_or_no_speech"]),
                    "is_safe_for_ads": bool(row["is_safe_for_ads"]),
                    "has_visible_watermark": bool(row["has_visible_watermark"]),
                    "reason": row["reason"],
                    "cached": True,
                }
        return None

    def set_cached_judge(
        self,
        file_hash: str,
        funny_score: float,
        is_english_or_no_speech: bool,
        is_safe_for_ads: bool,
        has_visible_watermark: bool,
        reason: str,
    ) -> None:
        """Cache Claude Vision evaluation."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO llm_judge_cache (
                    file_hash, funny_score, is_english_or_no_speech,
                    is_safe_for_ads, has_visible_watermark, reason, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                file_hash, funny_score,
                1 if is_english_or_no_speech else 0,
                1 if is_safe_for_ads else 0,
                1 if has_visible_watermark else 0,
                reason, now
            ))

    # --- Decision Logging ---
    def record_decision(
        self,
        post_id: str,
        file_hash: str,
        hybrid_score: float,
        status: str,
        decision_reason: str,
        target_date: str,
    ) -> None:
        """Log video acceptance/rejection decision to tuning table."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO agent_decisions (
                    post_id, file_hash, hybrid_score, status, decision_reason, target_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (post_id, file_hash, hybrid_score, status, decision_reason, target_date, now))

    # --- Judge Failure & Retry Management ---
    def record_judge_failed(self, candidate: Dict[str, Any]) -> None:
        """
        Record a failed judge evaluation for retry in the next run.
        Also unmarks the post from scraper/data/videos.db so pre-download
        ID deduplication does not permanently block it.
        """
        post_id = candidate.get("post_id", "unknown")
        source = candidate.get("source", "unknown")
        title = candidate.get("title", "")
        video_path = str(candidate.get("video_path") or "")
        reason = candidate.get("judge_reason") or "Judge call error"
        now = datetime.now().isoformat()

        # Serialize candidate dictionary (stripping non-serializable objects)
        clean_cand = {}
        for k, v in candidate.items():
            if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
                clean_cand[k] = v
        meta_json_str = json.dumps(clean_cand)

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO judge_retries (
                    post_id, source, title, video_path, meta_json, failure_reason, retry_count, created_at, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    retry_count = judge_retries.retry_count + 1,
                    failure_reason = excluded.failure_reason,
                    last_attempt_at = excluded.last_attempt_at;
            """, (post_id, source, title, video_path, meta_json_str, reason, now, now))

        # Unmark in scraper videos.db if present so deduplication doesn't block re-scrape
        try:
            scraper_db_path = Path(__file__).resolve().parent.parent.parent / "scraper" / "data" / "videos.db"
            if scraper_db_path.is_file():
                with sqlite3.connect(str(scraper_db_path), timeout=5.0) as s_conn:
                    s_conn.execute("DELETE FROM videos WHERE post_id = ?", (post_id,))
        except Exception as e:
            logger.debug(f"Could not unmark {post_id} in scraper videos.db: {e}")

    def pop_pending_judge_retries(self, max_retries: int = 3) -> List[Dict[str, Any]]:
        """
        Fetch candidate clips that failed judging previously and are eligible for retry.
        Verifies that video media file still exists on disk.
        """
        now = datetime.now().isoformat()
        retrying = []
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT post_id, video_path, meta_json, retry_count
                FROM judge_retries
                WHERE retry_count < ?
                ORDER BY created_at ASC
            """, (max_retries,))
            rows = cur.fetchall()

            for r in rows:
                p_id = r["post_id"]
                vid_path = Path(r["video_path"]) if r["video_path"] else None
                if vid_path and vid_path.is_file():
                    try:
                        cand_dict = json.loads(r["meta_json"])
                        cand_dict["is_retry"] = True
                        cand_dict["retry_count"] = r["retry_count"] + 1
                        # Reset failure flags for the new evaluation
                        cand_dict.pop("judge_failed", None)
                        if cand_dict.get("status") == "judge_failed":
                            cand_dict["status"] = "pending_judge"
                        retrying.append(cand_dict)
                    except Exception:
                        pass

            if retrying:
                conn.execute("""
                    UPDATE judge_retries
                    SET retry_count = retry_count + 1, last_attempt_at = ?
                    WHERE post_id IN ({})
                """.format(",".join("?" for _ in retrying)), [now] + [c["post_id"] for c in retrying])

        return retrying

    def clear_judge_retry(self, post_id: str) -> None:
        """Clear a candidate from judge_retries once a final decision is reached."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM judge_retries WHERE post_id = ?", (post_id,))

    # --- LLM API Logging ---
    def log_llm_call(
        self,
        task_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_s: float,
    ) -> None:
        """Log token count and dollar spend."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO llm_logs (
                    task_type, model, input_tokens, output_tokens, cost_usd, latency_s, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task_type, model, input_tokens, output_tokens, cost_usd, latency_s, now))

    # --- Reserve Pool Management ---
    def add_to_reserve_pool(self, video_data: Dict[str, Any]) -> None:
        """Add surplus approved video to reserve pool for future days."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO reserve_pool (
                    post_id, video_path, caption, title, description,
                    uploader, post_url, score, duration, resolution, status, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)
            """, (
                video_data["post_id"],
                video_data["video_path"],
                video_data.get("caption"),
                video_data.get("title"),
                video_data.get("description"),
                video_data.get("uploader"),
                video_data.get("post_url"),
                video_data.get("score"),
                video_data.get("duration"),
                video_data.get("resolution"),
                now,
            ))

    def fetch_reserve_pool_candidates(self, limit: int) -> List[Dict[str, Any]]:
        """Retrieve available reserve videos to backfill shortage."""
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT * FROM reserve_pool
                WHERE status = 'available'
                ORDER BY score DESC, added_at ASC
                LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def mark_reserve_used(self, post_ids: List[str]) -> None:
        """Mark reserved videos as used so they are not scheduled twice."""
        if not post_ids:
            return
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            placeholders = ",".join(["?"] * len(post_ids))
            conn.execute(f"""
                UPDATE reserve_pool
                SET status = 'used', used_at = ?
                WHERE post_id IN ({placeholders})
            """, [now] + post_ids)

    def mark_reserve_missing(self, post_ids: List[str]) -> None:
        """Mark reserved videos as missing if their files no longer exist on disk."""
        if not post_ids:
            return
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            placeholders = ",".join(["?"] * len(post_ids))
            conn.execute(f"""
                UPDATE reserve_pool
                SET status = 'missing', used_at = ?
                WHERE post_id IN ({placeholders})
            """, [now] + post_ids)

    # --- Schedule Management ---
    def save_schedule_slots(self, target_date: str, slots: List[Dict[str, Any]]) -> None:
        """Save final 24-hour schedule to SQLite database."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            for s in slots:
                conn.execute("""
                    INSERT OR REPLACE INTO schedule (
                        target_date, slot_hour, slot_time, post_id, video_path,
                        title, description, caption, source_url, uploader, status,
                        rights_status, rights_risk, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """, (
                    target_date,
                    s["slot_hour"],
                    s["slot_time"],
                    s["post_id"],
                    s["video_path"],
                    s.get("title", ""),
                    s.get("description", ""),
                    s.get("caption", ""),
                    s.get("source_url", ""),
                    s.get("uploader", ""),
                    s.get("rights_status", "cleared"),
                    s.get("rights_risk", 0),
                    now,
                ))

    # --- Phase 3.5: Dynamic Source Registry Management ---
    def seed_default_sources(
        self,
        subreddits: Optional[List[str]] = None,
        allowed_listings: Optional[List[str]] = None,
    ) -> None:
        """Seed default active subreddits and multi-window archival streams into sources table if empty."""
        default_subs = subreddits or [
            "funny",
            "Unexpected",
            "holdmybeer",
            "Whatcouldgowrong",
            "instant_regret",
            "oddlysatisfying",
            "AnimalsBeingDerps",
            "ContagiousLaughter",
            "Catculations",
            "IdiotsInCars",
            "therewasanattempt",
            "MadeMeSmile",
        ]
        default_listings = allowed_listings or ["hot", "top_week", "top_month", "top_year"]
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            for sub in default_subs:
                for l in default_listings:
                    conn.execute("""
                        INSERT OR IGNORE INTO sources (
                            source_type, name, listing_mode, status, added_by, created_at, updated_at
                        ) VALUES ('subreddit', ?, ?, 'active', 'config', ?, ?)
                    """, (sub, l, now, now))

    def get_sources_for_selection(
        self,
        total_budget: int = 10,
        active_ratio: float = 0.8,
        trial_ratio: float = 0.2,
        allowed_listings: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Select ~80% active sources (ranked by yield) and ~20% trial sources for current fetch."""
        self.seed_default_sources(allowed_listings=allowed_listings)
        num_active = max(1, int(round(total_budget * active_ratio)))
        num_trial = max(1, total_budget - num_active)
        now = datetime.now().isoformat()

        listings_filter = ""
        params_filter: List[Any] = []
        if allowed_listings:
            placeholders = ",".join(["?"] * len(allowed_listings))
            listings_filter = f"AND listing_mode IN ({placeholders})"
            params_filter = list(allowed_listings)

        with self._get_connection() as conn:
            # Auto-reset exhausted archive sources older than 7 days back to active
            conn.execute("""
                UPDATE sources
                SET status = 'active', consecutive_empty_runs = 0, pagination_cursor = NULL, updated_at = ?
                WHERE status = 'exhausted' AND (julianday(?) - julianday(updated_at)) >= 7.0
            """, (now, now))

            cur = conn.execute(f"""
                SELECT * FROM sources
                WHERE status = 'active' {listings_filter}
                ORDER BY yield_rate DESC, approved_count DESC, id ASC
                LIMIT ?
            """, params_filter + [num_active])
            active_rows = [dict(r) for r in cur.fetchall()]

            cur = conn.execute(f"""
                SELECT * FROM sources
                WHERE status = 'trial' {listings_filter}
                ORDER BY last_used_date ASC NULLS FIRST, id ASC
                LIMIT ?
            """, params_filter + [num_trial])
            trial_rows = [dict(r) for r in cur.fetchall()]

            if len(trial_rows) < num_trial:
                needed = num_trial - len(trial_rows)
                placeholders = ",".join([str(r["id"]) for r in active_rows]) if active_rows else "0"
                cur = conn.execute(f"""
                    SELECT * FROM sources
                    WHERE status = 'active' AND id NOT IN ({placeholders}) {listings_filter}
                    ORDER BY yield_rate DESC, id ASC
                    LIMIT ?
                """, params_filter + [needed])
                active_rows.extend([dict(r) for r in cur.fetchall()])

            selected = active_rows + trial_rows
            return selected[:total_budget]

    def get_deeper_archive_sources(
        self,
        loop_count: int = 2,
        limit: int = 4,
        allowed_listings: Optional[List[str]] = None,
        exclude_names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Phase 3.6: Fetch deeper pagination pages and alternative time windows
        for active sources before requesting new sources.
        Respects config listings: never requests top_month, top_year, or top_all unless
        explicitly present in allowed_listings.
        """
        self.seed_default_sources(allowed_listings=allowed_listings)
        valid_listings = list(allowed_listings or ["hot", "top_day", "top_week"])

        # Determine target archive listings from allowed_listings only
        archive_candidates = [l for l in valid_listings if l != "hot"]
        if not archive_candidates:
            archive_candidates = ["hot"]

        # Loop 2 targets top_week (or top_day if top_week not configured)
        # Loop 3 targets deeper pagination or other allowed listings
        if loop_count == 2:
            target_listings = [l for l in ["top_week", "top_day"] if l in archive_candidates] or archive_candidates
        else:
            target_listings = [l for l in ["top_week", "top_day", "hot"] if l in valid_listings] or archive_candidates

        # Strict guarantee: NEVER include heavy listings if not in allowed_listings
        target_listings = [l for l in target_listings if l in valid_listings]
        if not target_listings:
            target_listings = [valid_listings[0]]

        placeholders = ",".join(["?"] * len(target_listings))
        params: List[Any] = list(target_listings)

        exclude_clause = ""
        if exclude_names:
            ex_placeholders = ",".join(["?"] * len(exclude_names))
            exclude_clause = f"AND LOWER(name) NOT IN ({ex_placeholders})"
            params.extend([n.lower() for n in exclude_names])

        with self._get_connection() as conn:
            # 1. First, check active sources in target archive time windows (prioritizing existing cursors & untried subs)
            cur = conn.execute(f"""
                SELECT * FROM sources
                WHERE status = 'active' AND listing_mode IN ({placeholders}) {exclude_clause}
                ORDER BY (pagination_cursor IS NOT NULL) DESC, yield_rate DESC, id ASC
                LIMIT ?
            """, params + [limit])
            rows = [dict(r) for r in cur.fetchall()]

            if len(rows) < limit:
                # Supplement with active sources in target listings even if tried before (deeper pagination)
                existing_ids = [str(r["id"]) for r in rows] or ["0"]
                id_placeholders = ",".join(existing_ids)
                needed = limit - len(rows)
                cur = conn.execute(f"""
                    SELECT * FROM sources
                    WHERE status = 'active' AND id NOT IN ({id_placeholders}) AND listing_mode IN ({placeholders})
                    ORDER BY (pagination_cursor IS NOT NULL) DESC, yield_rate DESC, id ASC
                    LIMIT ?
                """, list(target_listings) + [needed])
                rows.extend([dict(r) for r in cur.fetchall()])

            return rows[:limit]

    def add_trial_sources(
        self,
        sources_list: List[Dict[str, Any]],
        run_date: str,
        max_daily_trials: int = 5,
    ) -> int:
        """Add newly discovered and verified trial sources, capped at 5 per day."""
        now = datetime.now().isoformat()
        added_count = 0
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT COUNT(*) as count FROM sources
                WHERE added_by = 'llm' AND date(created_at) = date(?)
            """, (now,))
            today_count = cur.fetchone()["count"]
            remaining_quota = max(0, max_daily_trials - today_count)

            for s in sources_list:
                if added_count >= remaining_quota:
                    break
                s_type = s.get("source_type", "subreddit")
                s_name = s.get("name", "").strip()
                s_mode = s.get("listing_mode", "hot")
                if not s_name:
                    continue

                try:
                    cur = conn.execute("""
                        INSERT INTO sources (
                            source_type, name, listing_mode, status, added_by, created_at, updated_at
                        ) VALUES (?, ?, ?, 'trial', 'llm', ?, ?)
                    """, (s_type, s_name, s_mode, now, now))
                    if cur.rowcount > 0:
                        added_count += 1
                except sqlite3.IntegrityError:
                    pass

        return added_count

    def update_source_metrics(
        self,
        source_type: str,
        name: str,
        listing_mode: str,
        found: int = 0,
        downloaded: int = 0,
        approved: int = 0,
        rejected: int = 0,
        reject_reasons: Optional[Dict[str, int]] = None,
        cursor: Optional[str] = None,
        run_date: Optional[str] = None,
    ) -> None:
        """
        Update source yield stats after final decision.
        Promotes trial sources (>=10 downloads, yield >= 0.30).
        Pauses underperforming sources (>=15 downloads, yield < 0.15).
        Marks exhausted archive sources (>=3 consecutive empty runs).
        """
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT * FROM sources
                WHERE source_type = ? AND name = ? AND listing_mode = ?
            """, (source_type, name, listing_mode))
            row = cur.fetchone()

            if not row:
                conn.execute("""
                    INSERT INTO sources (
                        source_type, name, listing_mode, status, found_count, downloaded_count,
                        approved_count, rejected_count, yield_rate, added_by, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 0.0, 'auto', ?, ?)
                """, (source_type, name, listing_mode, found, downloaded, approved, rejected, now, now))
                cur = conn.execute("""
                    SELECT * FROM sources
                    WHERE source_type = ? AND name = ? AND listing_mode = ?
                """, (source_type, name, listing_mode))
                row = cur.fetchone()

            new_found = row["found_count"] + found
            new_downloaded = row["downloaded_count"] + downloaded
            new_approved = row["approved_count"] + approved
            new_rejected = row["rejected_count"] + rejected

            if new_downloaded > 0:
                smoothed_yield = round((new_approved + 1.0) / (new_downloaded + 3.0), 4)
            else:
                smoothed_yield = 0.0

            current_reasons = {}
            if row["reject_reasons_json"]:
                try:
                    current_reasons = json.loads(row["reject_reasons_json"])
                except Exception:
                    current_reasons = {}
            if reject_reasons:
                for k, v in reject_reasons.items():
                    current_reasons[k] = current_reasons.get(k, 0) + v

            if found == 0:
                new_empty = row["consecutive_empty_runs"] + 1
            else:
                new_empty = 0

            status = row["status"]
            if new_empty >= 3 and listing_mode in ["top_week", "top_month", "top_year", "top_all"]:
                status = "exhausted"
            elif status == "trial" and new_downloaded >= 10:
                if smoothed_yield >= 0.30:
                    status = "active"
                    logger.info(f"Promoted source ({source_type}, {name}, {listing_mode}) to ACTIVE (yield={smoothed_yield:.2f})")
                elif smoothed_yield < 0.15:
                    status = "paused"
                    logger.info(f"Paused trial source ({source_type}, {name}, {listing_mode}) due to low yield ({smoothed_yield:.2f})")
            elif status == "active" and new_downloaded >= 15:
                if smoothed_yield < 0.15:
                    status = "paused"
                    logger.info(f"Paused active source ({source_type}, {name}, {listing_mode}) due to low yield ({smoothed_yield:.2f})")

            new_cursor = cursor or row["pagination_cursor"]
            if listing_mode == "hot":
                new_cursor = None
            conn.execute("""
                UPDATE sources
                SET found_count = ?, downloaded_count = ?, approved_count = ?, rejected_count = ?,
                    yield_rate = ?, consecutive_empty_runs = ?, pagination_cursor = ?,
                    reject_reasons_json = ?, status = ?, last_used_date = ?, updated_at = ?
                WHERE id = ?
            """, (
                new_found, new_downloaded, new_approved, new_rejected,
                smoothed_yield, new_empty, new_cursor,
                json.dumps(current_reasons), status, run_date or now, now,
                row["id"]
            ))

    def log_discovery(
        self,
        run_date: str,
        suggested: List[Dict[str, Any]],
        verified: List[Dict[str, Any]],
        rejected: List[Dict[str, Any]],
    ) -> None:
        """Record daily LLM discovery outcome."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO discovery_logs (run_date, suggested_by_llm_json, verified_json, rejected_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                run_date,
                json.dumps(suggested),
                json.dumps(verified),
                json.dumps(rejected),
                now,
            ))

    def get_performance_summary(self) -> Dict[str, Any]:
        """Aggregate stats for fresh context collector."""
        self.seed_default_sources()
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM sources WHERE status != 'exhausted' ORDER BY yield_rate DESC")
            all_sources = [dict(r) for r in cur.fetchall()]

            cur = conn.execute("SELECT DISTINCT name FROM sources")
            already_tried = [r["name"] for r in cur.fetchall()]

            cur = conn.execute("""
                SELECT decision_reason, COUNT(*) as cnt
                FROM agent_decisions
                WHERE status = 'rejected'
                GROUP BY decision_reason
                ORDER BY cnt DESC
                LIMIT 5
            """)
            top_reasons = [{"reason": r["decision_reason"], "count": r["cnt"]} for r in cur.fetchall()]

            return {
                "active_sources": [s for s in all_sources if s["status"] == "active"],
                "trial_sources": [s for s in all_sources if s["status"] == "trial"],
                "already_tried_names": already_tried,
                "top_reject_reasons": top_reasons,
            }

    # --- Run Stats & Daily Runs (Phase 5 Dashboard Instrumentation) ---
    def record_run_stats(self, stats: Dict[str, Any]) -> int:
        """Insert a run_stats record for a curation execution."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO run_stats (
                    target_date, found, dropped_prescreen, dropped_duration, downloaded,
                    dropped_filters, judged, rejected_judge, borderline, approved,
                    selected, reserve_added, reject_reasons_json, source_stats_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                stats.get("target_date"),
                stats.get("found", 0),
                stats.get("dropped_prescreen", 0),
                stats.get("dropped_duration", 0),
                stats.get("downloaded", 0),
                stats.get("dropped_filters", 0),
                stats.get("judged", 0),
                stats.get("rejected_judge", 0),
                stats.get("borderline", 0),
                stats.get("approved", 0),
                stats.get("selected", 0),
                stats.get("reserve_added", 0),
                json.dumps(stats.get("reject_reasons_json", {})),
                json.dumps(stats.get("source_stats_json", {})),
                now,
            ))
            return cursor.lastrowid

    def get_latest_run_stats(self, target_date: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest complete run_stats for target_date."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM run_stats
                WHERE target_date = ?
                ORDER BY id DESC LIMIT 1
            """, (target_date,)).fetchone()
            if row:
                d = dict(row)
                for k in ("reject_reasons_json", "source_stats_json"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            d[k] = {}
                return d
        return None

    def record_daily_run_start(self) -> int:
        """Record the beginning of a daily pipeline execution."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO daily_runs (started_at, status)
                VALUES (?, 'running')
            """, (now,))
            return cursor.lastrowid

    def record_daily_run_finish(
        self,
        run_id: int,
        status: str,
        uploaded: int = 0,
        scheduled: int = 0,
        failed: int = 0,
        message: str = "",
    ) -> None:
        """Update daily_runs upon run completion."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_runs
                SET finished_at = ?, status = ?, uploaded = ?, scheduled = ?, failed = ?, message = ?
                WHERE id = ?
            """, (now, status, uploaded, scheduled, failed, message, run_id))

    def get_last_daily_run(self) -> Optional[Dict[str, Any]]:
        """Retrieve most recent daily_runs record."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM daily_runs
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    # --- Phase 6: Rights Checks & Content ID Claims Persistence ---
    def record_rights_check(
        self,
        post_id: str,
        target_date: str,
        layers: Dict[str, Any],
        risk_score: int,
        decision: str,
        reasons: List[str],
    ) -> int:
        """Insert a rights_checks evaluation row."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO rights_checks (
                    post_id, target_date, checked_at, layers_json,
                    risk_score, decision, reasons_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                post_id,
                target_date,
                now,
                json.dumps(layers, default=str),
                risk_score,
                decision,
                json.dumps(reasons),
                now,
            ))
            return cur.lastrowid

    def get_rights_check(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve most recent rights check for post_id."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM rights_checks WHERE post_id = ? ORDER BY id DESC LIMIT 1
            """, (post_id,)).fetchone()
            if row:
                d = dict(row)
                d["layers"] = json.loads(d["layers_json"]) if d.get("layers_json") else {}
                d["reasons"] = json.loads(d["reasons_json"]) if d.get("reasons_json") else []
                return d
        return None

    def record_claim(
        self,
        post_id: str,
        claim_type: str,
        claimant: str,
        video_id: Optional[str] = None,
        asset_title: Optional[str] = None,
        found_in: Optional[str] = None,
        action: str = "held",
        source_name: Optional[str] = None,
        uploader: Optional[str] = None,
        phashes: Optional[List[str]] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Insert a Content ID claim row and auto-update upload status."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO claims (
                    video_id, post_id, claim_type, claimant, asset_title,
                    found_in, action, noted_at, source_name, uploader,
                    phashes_json, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id,
                post_id,
                claim_type,
                claimant,
                asset_title,
                found_in,
                action,
                now,
                source_name,
                uploader,
                json.dumps(phashes) if phashes else None,
                notes,
                now,
            ))
            claim_id = cur.lastrowid

            # Update uploads and schedule tables
            conn.execute("""
                UPDATE uploads SET claim_status = 'claimed', rights_status = 'claimed'
                WHERE post_id = ?
            """, (post_id,))
            conn.execute("""
                UPDATE schedule SET rights_status = 'claimed'
                WHERE post_id = ?
            """, (post_id,))

            return claim_id

    def get_claims(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve registered Content ID claims."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM claims ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def add_blocklist_entry(
        self,
        entry_type: str,
        value: str,
        source_claim_id: Optional[int] = None,
        status: str = "active",
    ) -> int:
        """Add learned or manual blocklist entry."""
        with self._get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO rights_blocklist (type, value, source_claim_id, status)
                VALUES (?, ?, ?, ?)
            """, (entry_type, value, source_claim_id, status))
            return cur.lastrowid

    def get_blocklist(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve blocklist entries."""
        with self._get_connection() as conn:
            if status:
                cur = conn.execute("SELECT * FROM rights_blocklist WHERE status = ? ORDER BY id DESC", (status,))
            else:
                cur = conn.execute("SELECT * FROM rights_blocklist ORDER BY id DESC")
            return [dict(r) for r in cur.fetchall()]

    def update_blocklist_status(self, entry_id: int, status: str) -> bool:
        """Update blocklist entry status ('active', 'disabled', 'pending_confirmation')."""
        with self._get_connection() as conn:
            cur = conn.execute("UPDATE rights_blocklist SET status = ? WHERE id = ?", (status, entry_id))
            return cur.rowcount > 0

    def increment_provider_usage(
        self,
        provider: str,
        count: int = 1,
        errors: int = 0,
        day: Optional[str] = None,
    ) -> None:
        """Increment daily API provider usage counter and error counter."""
        use_day = day or datetime.now().strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO provider_usage (day, provider, calls, errors)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, provider) DO UPDATE SET
                    calls = calls + ?,
                    errors = errors + ?
            """, (use_day, provider, count, errors, count, errors))

    def get_provider_usage(self, provider: Optional[str] = None, day: Optional[str] = None) -> Dict[str, Any]:
        """Get provider calls used today."""
        use_day = day or datetime.now().strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            if provider:
                row = conn.execute(
                    "SELECT calls, errors FROM provider_usage WHERE day = ? AND provider = ?",
                    (use_day, provider)
                ).fetchone()
                return dict(row) if row else {"calls": 0, "errors": 0}
            else:
                cur = conn.execute("SELECT provider, calls, errors FROM provider_usage WHERE day = ?", (use_day,))
                return {r["provider"]: {"calls": r["calls"], "errors": r["errors"]} for r in cur.fetchall()}

    def count_real_published(self, subreddit: Optional[str] = None) -> int:
        """
        Count real published clips on YouTube.
        A clip is considered real published if:
        1. It has a video_id and status != 'deleted'
        2. And either status in ('uploaded', 'published') or scheduled publish time has arrived (publish_at <= now or target_date <= today_ny).
        If subreddit is provided, filters to clips originating from that subreddit.
        """
        now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        today_ny = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            if subreddit:
                clean_sub = subreddit.strip().replace("r/", "").replace("/r/", "").strip().lower()
                query = """
                    SELECT COUNT(*) FROM uploads u
                    LEFT JOIN schedule s ON u.post_id = s.post_id
                    WHERE u.video_id IS NOT NULL
                      AND u.status != 'deleted'
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
                return conn.execute(query, (
                    now_utc_str,
                    today_ny,
                    f"%reddit.com/r/{clean_sub}/%",
                    f"%/r/{clean_sub}/%",
                )).fetchone()[0]
            else:
                query = """
                    SELECT COUNT(*) FROM uploads u
                    WHERE u.video_id IS NOT NULL
                      AND u.status != 'deleted'
                      AND (
                        u.status IN ('uploaded', 'published')
                        OR (u.publish_at IS NOT NULL AND u.publish_at <= ?)
                        OR (u.publish_at IS NULL AND u.target_date <= ?)
                      )
                """
                return conn.execute(query, (now_utc_str, today_ny)).fetchone()[0]

    def get_claim_rates_by_subreddit(self) -> Dict[str, Any]:
        """Compute claims per 100 published clips by subreddit."""
        with self._get_connection() as conn:
            cur_claims = conn.execute("""
                SELECT source_name, COUNT(*) as claim_count
                FROM claims
                WHERE source_name IS NOT NULL
                GROUP BY source_name
            """).fetchall()
            claims_by_source = {r["source_name"]: r["claim_count"] for r in cur_claims}

            total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            total_published = self.count_real_published()
            overall_rate = round((total_claims / total_published) * 100, 2) if total_published > 0 else 0.0

            return {
                "overall_claims_per_100": overall_rate,
                "total_claims": total_claims,
                "total_published": total_published,
                "by_source": claims_by_source,
            }


