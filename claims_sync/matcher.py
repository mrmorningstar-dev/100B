"""
claims_sync/matcher.py

Matches parsed claim emails against uploads in the pipeline database.
Enforces:
  1. Match by video_id
  2. Match by exact title
  3. Match by normalized title (ignoring hashtags like #shorts #funny)
  4. Safeguard: ONLY videos in the uploads table may match; ~30 legacy channel videos are ignored
  5. Deduplication by (video_id, asset_title, claimant, claim_type)
"""

import re
from typing import Optional, Dict, Any, Tuple
from loguru import logger
from claims_sync.parser import ParsedClaimEmail


def normalize_title_for_matching(title: Optional[str]) -> str:
    """
    Normalizes a video title by:
      - Converting to lowercase
      - Removing hashtags (e.g. #shorts, #funny, #comedy, #fails)
      - Stripping punctuation and extra whitespace
    """
    if not title:
        return ""
    # Strip hashtags
    no_tags = re.sub(r"#\w+", "", title)
    # Strip punctuation and normalize whitespace
    cleaned = re.sub(r"[^\w\s]", "", no_tags)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def track_titles_match(title1: Optional[str], title2: Optional[str]) -> bool:
    """
    Check if two track or asset titles match for claim merging.
    Compares normalized strings and handles common variants like (Remastered) or ' - Artist'.
    """
    if not title1 or not title2:
        return False
    t1 = title1.strip().lower()
    t2 = title2.strip().lower()
    if not t1 or not t2:
        return False
    if t1 in ("unknown", "unknown asset") or t2 in ("unknown", "unknown asset"):
        return False
    if t1 == t2:
        return True

    # Strip parenthetical annotations e.g. (Remastered 2020), [Official Audio]
    clean_p1 = re.sub(r"\s*[\(\[].*?[\)\]]", "", t1).strip()
    clean_p2 = re.sub(r"\s*[\(\[].*?[\)\]]", "", t2).strip()

    # Split on " - " or " by " artist suffix
    base1 = re.split(r"\s+(?:[-–—]|by)\s+", clean_p1)[0].strip()
    base2 = re.split(r"\s+(?:[-–—]|by)\s+", clean_p2)[0].strip()

    # Normalize punctuation and whitespace
    n1 = re.sub(r"[^\w\s]", "", base1)
    n2 = re.sub(r"[^\w\s]", "", base2)
    n1 = re.sub(r"\s+", " ", n1).strip()
    n2 = re.sub(r"\s+", " ", n2).strip()

    return bool(n1 and n2 and n1 == n2)


class ClaimsMatcher:
    def __init__(self, db):
        self.db = db

    def match_email_to_upload(self, parsed: ParsedClaimEmail) -> Optional[Dict[str, Any]]:
        """
        Match a parsed claim email against active uploads in the pipeline.
        Returns the uploads dict (with post_id, video_id, status, target_date, slot_time, title)
        or None if no match in uploads table.
        """
        with self.db._get_connection() as conn:
            # Fetch all uploads joined with schedule titles
            rows = conn.execute("""
                SELECT 
                    u.post_id,
                    u.video_id,
                    u.status,
                    u.target_date,
                    u.slot_time,
                    u.privacy,
                    u.publish_at,
                    u.rights_status,
                    u.claim_status,
                    s.title,
                    s.uploader
                FROM uploads u
                LEFT JOIN schedule s ON u.post_id = s.post_id
            """).fetchall()

            uploads = [dict(r) for r in rows]

        if not uploads:
            return None

        # Tier 1: Match by video_id
        if parsed.video_id:
            for up in uploads:
                if up.get("video_id") and up["video_id"] == parsed.video_id:
                    logger.info(f"Matched claim email to upload by video_id={parsed.video_id} (post_id={up['post_id']})")
                    return up

        # Tier 2: Match by exact title
        if parsed.video_title:
            clean_email_title = parsed.video_title.strip().lower()
            for up in uploads:
                db_title = (up.get("title") or "").strip().lower()
                if db_title and db_title == clean_email_title:
                    logger.info(f"Matched claim email to upload by exact title='{up['title']}' (post_id={up['post_id']})")
                    return up

        # Tier 3: Match by normalized title (ignoring #shorts #funny etc.)
        if parsed.video_title:
            norm_email_title = normalize_title_for_matching(parsed.video_title)
            if norm_email_title:
                for up in uploads:
                    norm_db_title = normalize_title_for_matching(up.get("title"))
                    if norm_db_title and norm_db_title == norm_email_title:
                        logger.info(f"Matched claim email to upload by normalized title='{norm_db_title}' (post_id={up['post_id']})")
                        return up

        return None

    def upsert_claim(
        self,
        upload: Dict[str, Any],
        parsed: ParsedClaimEmail,
        source: str = "email",
    ) -> Tuple[int, str]:
        """
        Insert or update a claim in the claims table, deduplicating by
        (video_id, asset_title, claimant, claim_type).

        If an incoming email claim matches a video with an existing manual
        placeholder claim (claimant 'unknown' or source 'manual'/'seed'),
        the email upgrades the manual placeholder in-place rather than
        creating a duplicate row.
        
        Returns:
            (claim_id, action_taken) where action_taken is 'created', 'updated', or 'released'
        """
        video_id = upload.get("video_id") or parsed.video_id
        post_id = upload["post_id"]
        asset_title = parsed.asset_title or "Unknown Asset"
        claimant = parsed.claimant or "Unknown Claimant"
        claim_type = parsed.claim_type or "audio"
        affects_reach = 1 if parsed.affects_reach else 0

        with self.db._get_connection() as conn:
            # 1. Exact match check
            existing = conn.execute("""
                SELECT id, state, claimant, asset_title, claim_type, source, track_title, track_artist FROM claims
                WHERE ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                  AND LOWER(COALESCE(asset_title, '')) = LOWER(?)
                  AND LOWER(COALESCE(claimant, '')) = LOWER(?)
                  AND LOWER(COALESCE(claim_type, '')) = LOWER(?)
            """, (video_id, post_id, asset_title, claimant, claim_type)).fetchone()

            # 2. Manual / Seed placeholder upgrade check:
            # If no exact match, look for an active placeholder claim for this video/post
            # with unknown claimant or manual/seed source, matching claim_type AND matching track name.
            if not existing:
                candidates = conn.execute("""
                    SELECT id, state, claimant, asset_title, claim_type, source, track_title, track_artist FROM claims
                    WHERE ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                      AND LOWER(COALESCE(claim_type, '')) = LOWER(?)
                      AND (
                          LOWER(COALESCE(claimant, '')) IN ('unknown', 'unknown claimant', '')
                          OR source IN ('manual', 'seed')
                      )
                      AND state IN ('active', 'unknown_pending_confirmation')
                    ORDER BY id ASC
                """, (video_id, post_id, claim_type)).fetchall()

                incoming_tracks = [t for t in [asset_title, getattr(parsed, "track_title", None)] if t]
                for cand in candidates:
                    cand_tracks = [t for t in [cand["asset_title"], cand["track_title"]] if t]
                    if any(track_titles_match(it, ct) for it in incoming_tracks for ct in cand_tracks):
                        existing = cand
                        break

            if parsed.event_type == "claim_released" or (parsed.event_type == "dispute_outcome" and parsed.dispute_released):
                # Release claim
                # If exact match wasn't found by (video_id, asset, claimant, type),
                # find by video_id and claimant, or any active/pending claim for this video
                if not existing:
                    existing = conn.execute("""
                        SELECT id, state, claimant, asset_title, claim_type, source FROM claims
                        WHERE ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                          AND (LOWER(COALESCE(claimant, '')) = LOWER(?) OR LOWER(COALESCE(claimant, '')) IN ('unknown', 'unknown claimant', '') OR ? = 'unknown')
                          AND state IN ('active', 'unknown_pending_confirmation')
                    """, (video_id, post_id, claimant, claimant)).fetchone()

                if not existing:
                    existing = conn.execute("""
                        SELECT id, state, claimant, asset_title, claim_type, source FROM claims
                        WHERE ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                          AND state IN ('active', 'unknown_pending_confirmation')
                    """, (video_id, post_id)).fetchone()

                if existing:
                    claim_id = existing["id"]
                    new_claimant = claimant if claimant.lower() not in ("unknown", "unknown claimant", "") else existing["claimant"]
                    conn.execute("""
                        UPDATE claims
                        SET state = 'released',
                            claimant = ?,
                            released_at = CURRENT_TIMESTAMP,
                            last_seen_at = CURRENT_TIMESTAMP,
                            message_id = COALESCE(?, message_id)
                        WHERE id = ?
                    """, (new_claimant, parsed.message_id, claim_id))
                else:
                    cur = conn.execute("""
                        INSERT INTO claims (
                            video_id, post_id, claim_type, claimant, asset_title, action,
                            state, source, noted_at, detected_at, released_at, last_seen_at,
                            message_id, track_title, track_artist, affects_reach
                        ) VALUES (?, ?, ?, ?, ?, 'released', 'released', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                    """, (
                        video_id, post_id, claim_type, claimant, asset_title,
                        source, parsed.message_id, parsed.asset_title, parsed.track_artist, affects_reach
                    ))
                    claim_id = cur.lastrowid

                # Update uploads claim_status if no active claims remain
                active_claims = conn.execute("""
                    SELECT COUNT(*) as cnt FROM claims
                    WHERE ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                      AND state IN ('active', 'unknown_pending_confirmation')
                """, (video_id, post_id)).fetchone()["cnt"]

                if active_claims == 0:
                    conn.execute("""
                        UPDATE uploads SET claim_status = 'released' WHERE post_id = ?
                    """, (post_id,))
                    conn.execute("""
                        UPDATE schedule SET rights_status = 'cleared' WHERE post_id = ? AND rights_status = 'held'
                    """, (post_id,))

                return claim_id, "released"

            else:
                # Active claim (added or updated)
                if existing:
                    claim_id = existing["id"]
                    # Upgrade fields: if current values are unknown or manual placeholder, upgrade with email facts
                    new_claimant = claimant if claimant.lower() not in ("unknown", "unknown claimant", "") else existing["claimant"]
                    new_asset = asset_title if asset_title.lower() not in ("unknown", "unknown asset", "") else existing["asset_title"]
                    new_claim_type = claim_type if claim_type else existing["claim_type"]
                    new_source = source if source != "manual" else existing["source"]

                    conn.execute("""
                        UPDATE claims
                        SET state = 'active',
                            claimant = ?,
                            asset_title = COALESCE(?, asset_title),
                            claim_type = COALESCE(?, claim_type),
                            last_seen_at = CURRENT_TIMESTAMP,
                            message_id = COALESCE(?, message_id),
                            track_title = COALESCE(?, track_title),
                            track_artist = COALESCE(?, track_artist),
                            affects_reach = ?,
                            source = ?
                        WHERE id = ?
                    """, (
                        new_claimant,
                        new_asset,
                        new_claim_type,
                        parsed.message_id,
                        parsed.asset_title,
                        parsed.track_artist,
                        affects_reach,
                        new_source,
                        claim_id
                    ))
                    action_taken = "updated"
                else:
                    cur = conn.execute("""
                        INSERT INTO claims (
                            video_id, post_id, claim_type, claimant, asset_title, action,
                            state, source, noted_at, detected_at, last_seen_at,
                            message_id, track_title, track_artist, affects_reach
                        ) VALUES (?, ?, ?, ?, ?, 'held', 'active', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                    """, (
                        video_id, post_id, claim_type, claimant, asset_title,
                        source, parsed.message_id, parsed.asset_title, parsed.track_artist, affects_reach
                    ))
                    claim_id = cur.lastrowid
                    action_taken = "created"

                # Ensure any duplicate manual/placeholder row on this video with matching track name is removed
                existing_manuals = conn.execute("""
                    SELECT id, asset_title, track_title, claim_type FROM claims
                    WHERE id != ?
                      AND ((video_id = ? AND video_id IS NOT NULL AND video_id != '') OR post_id = ?)
                      AND LOWER(COALESCE(claim_type, '')) = LOWER(?)
                      AND (
                          LOWER(COALESCE(claimant, '')) IN ('unknown', 'unknown claimant', '')
                          OR source IN ('manual', 'seed')
                      )
                """, (claim_id, video_id, post_id, claim_type)).fetchall()

                incoming_tracks = [t for t in [asset_title, getattr(parsed, "track_title", None)] if t]
                for m_row in existing_manuals:
                    m_tracks = [t for t in [m_row["asset_title"], m_row["track_title"]] if t]
                    if any(track_titles_match(it, ct) for it in incoming_tracks for ct in m_tracks):
                        conn.execute("DELETE FROM claims WHERE id = ?", (m_row["id"],))

                # Update uploads table claim_status
                conn.execute("""
                    UPDATE uploads SET claim_status = 'claimed' WHERE post_id = ?
                """, (post_id,))

                return claim_id, action_taken

    def record_processed_email(
        self,
        parsed: ParsedClaimEmail,
        received_at: str,
        subject: str,
        sender: str,
        matched_post_id: Optional[str] = None,
        matched_video_id: Optional[str] = None,
        status: str = "processed",
    ) -> None:
        """
        Record message_id in claim_emails for idempotent tracking.
        Enforces storing only parsed fields and snippet <= 300 chars.
        """
        with self.db._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO claim_emails (
                    message_id, thread_id, received_at, subject, sender,
                    event_type, matched_post_id, matched_video_id,
                    status, raw_snippet
                ) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                parsed.message_id,
                received_at,
                subject[:250],
                sender[:150],
                parsed.event_type,
                matched_post_id,
                matched_video_id,
                status,
                parsed.raw_snippet[:300],
            ))

    def is_email_processed(self, message_id: str) -> bool:
        """Check if message_id was already processed."""
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT 1 FROM claim_emails WHERE message_id = ?", (message_id,)).fetchone()
            return row is not None
