#!/usr/bin/env python3
"""
100B Automated YouTube Shorts Pipeline - Claims Sync CLI
Commands:
  python claims_sync.py auth     - One-time interactive OAuth2 authorization for Gmail read-only
  python claims_sync.py status   - Inspect Gmail auth status, last sync, claims metrics
  python claims_sync.py inspect  - Query Gmail for YouTube copyright emails and display redacted structure
  python claims_sync.py sync     - Run full sync: fetch, match, update lifecycle, execute auto-action
"""

import sys
import argparse
from pathlib import Path
from loguru import logger

# Ensure project root and agent are in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR / "agent") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "agent"))

from claims_sync.auth import (
    authenticate_gmail_interactive,
    get_gmail_service,
    is_gmail_authenticated,
    TOKEN_GMAIL_PATH,
    CLIENT_SECRET_PATH,
)
from claims_sync.client import search_youtube_claim_messages


def cmd_auth(args):
    """Run interactive Gmail OAuth authorization."""
    print("=" * 65)
    print("  🔑 Initiating Gmail Read-Only OAuth Authorization")
    print("  Scope: https://www.googleapis.com/auth/gmail.readonly")
    print(f"  Token destination: {TOKEN_GMAIL_PATH}")
    print("=" * 65)
    try:
        creds = authenticate_gmail_interactive(direct_code=args.code)
        print("\n✅ Authorization successful! token_gmail.json saved.")
    except Exception as e:
        print(f"\n❌ Authorization failed: {e}")
        sys.exit(1)


def cmd_status(args):
    """Check Gmail connection and sync status."""
    print("=" * 65)
    print("  📊 Claims Sync Status")
    print("=" * 65)
    has_token = TOKEN_GMAIL_PATH.is_file()
    is_auth = is_gmail_authenticated()

    print(f"  Client Secret:  {'✅ Present' if CLIENT_SECRET_PATH.is_file() else '❌ Missing'} ({CLIENT_SECRET_PATH})")
    print(f"  Gmail Token:    {'✅ Present' if has_token else '❌ Missing'} ({TOKEN_GMAIL_PATH})")
    print(f"  Auth Status:    {'🟢 Connected & Valid' if is_auth else '🔴 Not Connected / Expired'}")

    if is_auth:
        try:
            service = get_gmail_service()
            profile = service.users().getProfile(userId="me").execute()
            print(f"  Account Email:  {profile.get('emailAddress')}")
            print(f"  Total Messages: {profile.get('messagesTotal')}")
        except Exception as e:
            print(f"  Service Error:  {e}")
    else:
        print("\n  👉 To connect, run: python claims_sync.py auth")


def cmd_inspect(args):
    """Fetch real YouTube copyright emails and display redacted samples."""
    if not is_gmail_authenticated():
        print("❌ Gmail is not authenticated. Please run 'python claims_sync.py auth' first.")
        sys.exit(1)

    service = get_gmail_service()
    messages = search_youtube_claim_messages(service, max_results=args.limit)

    print(f"\nFound {len(messages)} matching YouTube copyright/claim message(s):\n")
    if not messages:
        print("No messages found matching search query.")
        return

    for idx, msg in enumerate(messages, 1):
        print(f"--- [Message {idx}/{len(messages)}] ID: {msg['id']} ---")
        print(f"Date:    {msg['date']}")
        print(f"From:    {msg['sender']}")
        print(f"Subject: {msg['subject']}")
        print(f"Snippet: {msg['snippet']}")
        print("\nStructure Summary:")
        print(f"  - Body text length: {len(msg['body_text'])} chars")
        print(f"  - Body HTML length: {len(msg['body_html'])} chars")
        lines = [line.strip() for line in msg['body_text'].splitlines() if line.strip()]
        preview = lines[:15]
        print("  - Text Preview (first 15 lines):")
        for pline in preview:
            print(f"    | {pline}")
        print("\n" + "=" * 65 + "\n")


def cmd_sync(args):
    """Run full sync pipeline."""
    from claims_sync.sync import run_claims_sync
    from claims_sync.config import set_dry_run

    if args.dry_run:
        set_dry_run(True)

    print("=" * 65)
    print("  🔄 Starting Claims Sync (Gmail & YouTube Probe)")
    print("=" * 65)
    res = run_claims_sync()
    if res.get("success"):
        print(f"\n✅ Sync completed successfully:")
        print(f"  Processed Claims: {res.get('processed_count', 0)}")
        print(f"  Unmatched Emails: {res.get('unmatched_count', 0)}")
        print(f"  Missing Videos:   {res.get('missing_videos_detected', 0)}")
        print(f"  Actions Taken:    {len(res.get('actions_taken', []))}")
    else:
        print(f"\n❌ Sync halted: {res.get('error')}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="100B Claims Sync CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Auth
    p_auth = subparsers.add_parser("auth", help="Interactive OAuth authorization for Gmail")
    p_auth.add_argument("--code", type=str, default=None, help="Direct authorization code or redirect callback URL")
    p_auth.set_defaults(func=cmd_auth)

    # Status
    p_status = subparsers.add_parser("status", help="Inspect Gmail auth status and sync metrics")
    p_status.set_defaults(func=cmd_status)

    # Inspect
    p_inspect = subparsers.add_parser("inspect", help="Fetch and inspect real YouTube copyright emails")
    p_inspect.add_argument("--limit", type=int, default=5, help="Max messages to fetch")
    p_inspect.set_defaults(func=cmd_inspect)

    # Sync
    p_sync = subparsers.add_parser("sync", help="Run claim sync and automatic action")
    p_sync.add_argument("--dry-run", action="store_true", help="Force dry run mode")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
