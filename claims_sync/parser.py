"""
claims_sync/parser.py

Parses YouTube copyright notifications, Content ID claims, dispute outcomes,
and copyright strike emails from Gmail (read-only), as well as manual pastes
from YouTube Studio.
"""

import html
import re
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup


@dataclass
class ParsedClaimEmail:
    message_id: str
    event_type: str  # 'claim_added', 'claim_released', 'dispute_outcome', 'strike', 'noise', 'unknown'
    video_title: Optional[str] = None
    video_id: Optional[str] = None
    asset_title: Optional[str] = None
    track_artist: Optional[str] = None
    claimant: Optional[str] = None
    claim_type: str = "unknown"  # 'audio', 'visual', 'both', 'unknown'
    affects_reach: bool = False
    is_strike: bool = False
    dispute_released: bool = False
    raw_snippet: str = ""  # Strictly <= 300 characters
    is_noise: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize_spaces(text: str) -> str:
    """Replace all runs of whitespace (newlines, tabs, spaces) with a single space."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_claim_email(
    message_id: str,
    subject: str,
    body_text: str,
    body_html: str = "",
    snippet: str = "",
) -> ParsedClaimEmail:
    """
    Parse a Gmail message payload from YouTube regarding copyright / Content ID.
    Enforces that only parsed fields and a truncated <=300 char snippet are stored.
    Never stores or logs the full raw body.
    """
    clean_subject = (subject or "").strip()
    clean_snippet = (snippet or "").strip()
    truncated_snippet = clean_snippet[:300] if clean_snippet else (_normalize_spaces(body_text)[:300] if body_text else "")

    # Normalize whitespace for multi-line regexes and phrase matching
    norm_text = _normalize_spaces(body_text)
    norm_html = _normalize_spaces(body_html)
    combined_all = f"{clean_subject} {norm_text} {norm_html}"
    combined_lower = combined_all.lower()

    # 1. Noise check (Newsletters, Creator monthly reports, updates)
    if any(k in clean_subject.lower() for k in ["newsletter", "month in review", "year on youtube", "creator weekly"]):
        return ParsedClaimEmail(
            message_id=message_id,
            event_type="noise",
            raw_snippet=truncated_snippet,
            is_noise=True,
        )

    # 2. Strike detection
    # A true copyright strike explicitly mentions strike/takedown without the phrase "not a copyright strike"
    is_strike = False
    has_strike_keywords = any(k in combined_lower for k in ["copyright strike", "takedown notice", "removed due to a copyright"])
    is_not_strike = ("not a copyright strike" in combined_lower) or ("isn't a copyright strike" in combined_lower)

    if has_strike_keywords and not is_not_strike:
        is_strike = True

    # 3. Video ID extraction from links (search across text and html)
    video_id = None
    all_content_for_links = f"{body_text}\n{body_html}"
    m_vid = re.search(r"studio\.youtube\.com/video/([a-zA-Z0-9_-]{11})", all_content_for_links)
    if not m_vid:
        m_vid = re.search(r"youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})", all_content_for_links)
    if not m_vid:
        m_vid = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", all_content_for_links)
    if m_vid:
        video_id = m_vid.group(1)

    # 4. Video Title extraction
    video_title = None
    sub_prefixes = [
        r"Video cannot be monetised:\s*(.+)",
        r"Video is blocked in some territories:\s*(.+)",
        r"Video is blocked:\s*(.+)",
        r"Copyright claim on your video:\s*(.+)",
        r"Copyright claim on your Short:\s*(.+)",
        r"Copyright claim on:\s*(.+)",
        r"Copyright strike:\s*(.+)",
        r"Dispute outcome:\s*(.+)",
    ]
    for pat in sub_prefixes:
        m_sub = re.match(pat, clean_subject, re.IGNORECASE)
        if m_sub:
            video_title = html.unescape(_normalize_spaces(m_sub.group(1)))
            break

    if not video_title:
        m_title = re.search(
            r"(?:in your (?:video|Short)|found in your video|detected in your Short|detected in your video|regarding your video)\s*['‘“\"](.+?)['’”\"]\.\s*(?:The|This|As|Your)",
            norm_text,
            re.IGNORECASE,
        )
        if not m_title:
            m_title = re.search(
                r"(?:in your (?:video|Short)|found in your video|detected in your Short|detected in your video|regarding your video)\s*['‘“\"]([^'‘“\"]+)['’”\"]",
                norm_text,
                re.IGNORECASE,
            )
        if m_title:
            video_title = html.unescape(_normalize_spaces(m_title.group(1)))

    # 5. Asset Title and Track Artist extraction
    asset_title = None
    track_artist = None

    if body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        # In YouTube's Content ID template, the asset is styled with font-weight:600
        # and the artist is styled with font-weight:400 preceded by "- " or "&nbsp;-&nbsp;"
        asset_span = soup.find("span", style=re.compile(r"font-weight:\s*600"))
        if asset_span:
            asset_title = _normalize_spaces(asset_span.get_text())
            artist_span = asset_span.find_next_sibling("span")
            if artist_span:
                raw_artist = artist_span.get_text()
                raw_artist = re.sub(r"^[-\s–—\xa0]+", "", raw_artist).strip()
                if raw_artist:
                    track_artist = _normalize_spaces(raw_artist)

    # Text fallback if HTML parsing didn't find asset
    if not asset_title and "CONTENT USED" in body_text:
        content_sec = body_text.split("CONTENT USED", 1)[1].split("Content owners", 1)[0]
        m_raw_asset = re.search(r'font-weight:600;">([^<]+)</span>(?:<span[^>]*font-weight:400;">\s*(?:&nbsp;)?-(?:&nbsp;)?([^<]+)</span>)?', content_sec)
        if m_raw_asset:
            asset_title = html.unescape(_normalize_spaces(m_raw_asset.group(1)))
            if m_raw_asset.group(2):
                track_artist = html.unescape(_normalize_spaces(m_raw_asset.group(2)))
        else:
            lines = [line.strip() for line in content_sec.splitlines() if line.strip() and not line.strip().startswith("<")]
            if lines:
                parts = lines[0].split(" - ", 1)
                asset_title = _normalize_spaces(parts[0])
                if len(parts) > 1:
                    track_artist = _normalize_spaces(parts[1])

    # 6. Claimant / Content Owner extraction
    claimant = None
    m_claimant = re.search(r"Content owners?:\s*([^\r\n<]+)", body_text, re.IGNORECASE)
    if not m_claimant:
        m_claimant = re.search(r"Copyright owner:\s*([^\r\n<]+)", body_text, re.IGNORECASE)
    if not m_claimant:
        m_claimant = re.search(r"Claimant:\s*([^\r\n<]+)", body_text, re.IGNORECASE)
    if m_claimant:
        claimant = html.unescape(_normalize_spaces(m_claimant.group(1)))

    # 7. Claim Type extraction (audio, visual, both)
    claim_type = "unknown"
    m_type = re.search(r"(?:Content type|Type):\s*([^\r\n<]+)", body_text, re.IGNORECASE)
    if m_type:
        raw_type = m_type.group(1).strip().lower()
        if "audio" in raw_type and ("video" in raw_type or "visual" in raw_type):
            claim_type = "both"
        elif "audio" in raw_type:
            claim_type = "audio"
        elif "video" in raw_type or "visual" in raw_type:
            claim_type = "visual"
    else:
        if "copyright audio" in combined_lower or "audio claim" in combined_lower:
            claim_type = "audio"
        elif "copyright visual" in combined_lower or "video claim" in combined_lower:
            claim_type = "visual"

    # 8. Affects Reach / Impact on video
    affects_reach = False
    m_impact = re.search(r"Impact on video:\s*([^\r\n<]+)", body_text, re.IGNORECASE)
    if m_impact:
        impact_text = m_impact.group(1).strip().lower()
        if any(b in impact_text for b in ["blocked", "restricted", "takedown", "removed"]):
            affects_reach = True
        elif "no impact" in impact_text:
            affects_reach = False
    else:
        if "video is blocked" in clean_subject.lower() or "blocked globally" in combined_lower or "blocked in" in combined_lower:
            affects_reach = True

    # 9. Event Type & Lifecycle determination
    event_type = "unknown"
    dispute_released = False

    is_dispute_context = (
        "dispute" in clean_subject.lower()
        or "dispute outcome" in combined_lower
        or "your dispute" in combined_lower
        or "dispute was" in combined_lower
        or "dispute has been" in combined_lower
        or "disputed content" in combined_lower
    )

    if is_strike:
        event_type = "strike"
    elif is_dispute_context:
        event_type = "dispute_outcome"
        # Constraint: Only treat a dispute email as a release if the text explicitly says released or removed
        if "released" in combined_lower or "removed" in combined_lower:
            dispute_released = True
    elif any(r in combined_lower for r in [
        "claim was released",
        "claim has been released",
        "claim on your video was released",
        "claim has been removed",
        "claim was removed",
        "copyright claim was released",
        "copyright claim on your video has been released",
    ]):
        event_type = "claim_released"
    elif any(a in combined_lower for a in [
        "claimed content was found",
        "copyrighted content has been detected",
        "copyright-protected content has been detected",
        "video cannot be monetised",
        "video is blocked",
        "copyright claim",
    ]):
        event_type = "claim_added"

    # Final sanity check: if no title and no asset and not strike/released, mark as noise
    if not video_title and not asset_title and event_type == "unknown":
        return ParsedClaimEmail(
            message_id=message_id,
            event_type="noise",
            raw_snippet=truncated_snippet,
            is_noise=True,
        )

    return ParsedClaimEmail(
        message_id=message_id,
        event_type=event_type,
        video_title=video_title,
        video_id=video_id,
        asset_title=asset_title,
        track_artist=track_artist,
        claimant=claimant,
        claim_type=claim_type,
        affects_reach=affects_reach,
        is_strike=is_strike,
        dispute_released=dispute_released,
        raw_snippet=truncated_snippet,
        is_noise=False,
    )


def parse_studio_paste(paste_text: str) -> Dict[str, Any]:
    """
    Parse text copied directly from YouTube Studio claim details modal or table.
    Extracts: asset_title, track_artist, claimant, claim_type, affects_reach.
    """
    if not paste_text or not paste_text.strip():
        return {}

    text = paste_text.strip()
    text_lower = text.lower()

    # Track / Asset & Artist
    asset_title = None
    track_artist = None

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Check for "Song - Artist" format
    m_pair = re.search(r"([^\r\n–—-]+)\s*[-–—]\s*([^\r\n]+)", text)
    if m_pair and not any(kw in m_pair.group(0).lower() for kw in ["content used", "impact", "type:", "claim"]):
        asset_title = _normalize_spaces(m_pair.group(1))
        track_artist = _normalize_spaces(m_pair.group(2))
    elif len(lines) >= 2 and not any(kw in lines[0].lower() for kw in ["claim", "content", "copyright", "impact", "type"]):
        asset_title = _normalize_spaces(lines[0])
        if not any(kw in lines[1].lower() for kw in ["content", "claim", "copyright", "impact", "type", "audio", "video"]):
            track_artist = _normalize_spaces(lines[1])

    # Claimant
    claimant = None
    m_claimant = re.search(r"(?:Claimant|Content owners?|Claimed by|Owner):\s*([^\r\n]+)", text, re.IGNORECASE)
    if m_claimant:
        claimant = _normalize_spaces(m_claimant.group(1))
    else:
        for label in ["UMG", "Universal Music Group", "WMG", "Warner Music Group", "Sony Music Entertainment", "SME", "Believe Music", "Merlin"]:
            if label.lower() in text_lower:
                claimant = label
                break

    # Claim Type
    claim_type = "unknown"
    m_type = re.search(r"(?:Content type|Content used|Type|Claim type):\s*([^\r\n]+)", text, re.IGNORECASE)
    if m_type:
        raw_t = m_type.group(1).lower()
        if "audio" in raw_t and ("video" in raw_t or "visual" in raw_t):
            claim_type = "both"
        elif "audio" in raw_t:
            claim_type = "audio"
        elif "video" in raw_t or "visual" in raw_t:
            claim_type = "visual"
    else:
        # Fallback only if not in "Impact on video" context
        type_context = re.sub(r"impact on video[^\r\n]*", "", text_lower)
        if "audio" in type_context and ("visual" in type_context or "video" in type_context):
            claim_type = "both"
        elif "audio" in type_context or "melody" in type_context or "sound recording" in type_context:
            claim_type = "audio"
        elif "visual" in type_context or "video" in type_context:
            claim_type = "visual"

    # Reach impact
    affects_reach = False
    if any(b in text_lower for b in ["blocked", "restricted", "cannot be viewed"]):
        affects_reach = True
    elif "no impact" in text_lower or "visibility not affected" in text_lower:
        affects_reach = False

    return {
        "asset_title": asset_title,
        "track_artist": track_artist,
        "claimant": claimant,
        "claim_type": claim_type,
        "affects_reach": affects_reach,
    }
