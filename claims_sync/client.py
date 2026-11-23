import base64
from typing import List, Dict, Any, Optional, Tuple
from loguru import logger
from googleapiclient.discovery import Resource


DEFAULT_GMAIL_SEARCH_QUERY = 'from:(youtube.com) (claim OR copyright OR "Content ID" OR "Content-ID")'


def extract_body_parts(payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    Recursively extract (plain_text, html_text) from a Gmail message payload.
    """
    body_text = ""
    body_html = ""

    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if "body" in payload and "data" in payload["body"]:
        raw_data = payload["body"]["data"]
        try:
            decoded = base64.urlsafe_b64decode(raw_data).decode("utf-8", errors="replace")
            if "html" in mime_type:
                body_html += decoded
            else:
                body_text += decoded
        except Exception as e:
            logger.debug(f"Failed decoding payload body: {e}")

    for part in parts:
        sub_text, sub_html = extract_body_parts(part)
        body_text += ("\n" + sub_text if sub_text else "")
        body_html += ("\n" + sub_html if sub_html else "")

    return body_text.strip(), body_html.strip()


def search_youtube_claim_messages(
    service: Resource,
    query: Optional[str] = None,
    max_results: int = 10,
    after_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search Gmail for YouTube copyright / Content ID claim emails.
    Returns list of dicts with id, thread_id, subject, from, date, snippet, body_text, body_html.
    """
    q = query or DEFAULT_GMAIL_SEARCH_QUERY
    if after_date:
        q += f" after:{after_date}"

    logger.info(f"Querying Gmail API: {q}")
    results = service.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
    messages = results.get("messages", [])

    items = []
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        try:
            full_msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            payload = full_msg.get("payload", {})
            headers_list = payload.get("headers", [])
            headers = {h["name"].lower(): h["value"] for h in headers_list}

            body_text, body_html = extract_body_parts(payload)

            items.append({
                "id": msg_id,
                "thread_id": full_msg.get("threadId"),
                "snippet": full_msg.get("snippet", ""),
                "subject": headers.get("subject", ""),
                "sender": headers.get("from", ""),
                "date": headers.get("date", ""),
                "body_text": body_text,
                "body_html": body_html,
            })
        except Exception as e:
            logger.warning(f"Error fetching message {msg_id}: {e}")

    return items
