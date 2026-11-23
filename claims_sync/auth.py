import os
import http.server
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from loguru import logger

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

# Strictly read-only Gmail scope; isolated from YouTube upload/manage tokens
GMAIL_READONLY_SCOPE = ["https://www.googleapis.com/auth/gmail.readonly"]

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECRETS_DIR = WORKSPACE_ROOT / "uploader" / "secrets"
TOKEN_GMAIL_PATH = DEFAULT_SECRETS_DIR / "token_gmail.json"
CLIENT_SECRET_PATH = DEFAULT_SECRETS_DIR / "client_secret.json"


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return

        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs:
            _OAuthCallbackHandler.auth_error = qs["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization Denied</h1><p>You may close this tab.</p>")
            return

        if "code" in qs:
            _OAuthCallbackHandler.auth_code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                "<!DOCTYPE html><html><body style='font-family: sans-serif; text-align: center; padding: 40px;'>"
                "<h1 style='color: #2E7D4F;'>&#10004; Authentication Successful!</h1>"
                "<p style='font-size: 16px; color: #4B5563;'>Gmail read-only token has been authorized.</p>"
                "<p style='color: #6B7280; font-size: 14px;'>You can close this window now.</p>"
                "</body></html>"
            )
            self.wfile.write(html.encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h1>Listening for Google OAuth callback...</h1>")

    def log_message(self, format, *args):
        pass  # Silence stderr noise


def get_gmail_secrets_paths(secrets_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    """Return paths to (client_secret.json, token_gmail.json)."""
    sdir = Path(secrets_dir or DEFAULT_SECRETS_DIR).resolve()
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir / "client_secret.json", sdir / "token_gmail.json"


def is_gmail_authenticated(secrets_dir: Optional[Path] = None) -> bool:
    """Check if token_gmail.json exists and contains a valid refresh token."""
    _, token_path = get_gmail_secrets_paths(secrets_dir)
    if not token_path.is_file():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_READONLY_SCOPE)
        return bool(creds and (creds.valid or creds.refresh_token))
    except Exception:
        return False


def authenticate_gmail_interactive(
    secrets_dir: Optional[Path] = None,
    direct_code: Optional[str] = None,
    open_browser: bool = True,
) -> Credentials:
    """
    Run OAuth2 authorization in the user's browser.
    Requests strictly https://www.googleapis.com/auth/gmail.readonly.
    Saves credentials to uploader/secrets/token_gmail.json.
    """
    # Allow local HTTP for localhost redirect
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    client_secret_path, token_path = get_gmail_secrets_paths(secrets_dir)

    if not client_secret_path.is_file():
        candidates = list(client_secret_path.parent.glob("client_secret*.json"))
        if candidates:
            client_secret_path = candidates[0]
        else:
            err = (
                f"OAuth client secret file not found at: {client_secret_path}\n"
                f"Please ensure client_secret.json is placed in '{client_secret_path.parent}'."
            )
            logger.error(err)
            raise FileNotFoundError(err)

    logger.info(f"Initiating Gmail read-only OAuth flow using: {client_secret_path}")
    logger.info("Scope: https://www.googleapis.com/auth/gmail.readonly")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), GMAIL_READONLY_SCOPE)
    flow.redirect_uri = "http://localhost:8080/"

    # If code or redirect URL is passed directly:
    if direct_code:
        clean_code = direct_code.strip()
        if "code=" in clean_code:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(clean_code).query)
            clean_code = qs.get("code", [clean_code])[0]
        logger.info("Exchanging provided authorization code for tokens...")
        flow.fetch_token(code=clean_code)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(flow.credentials.to_json())
        logger.success(f"Gmail OAuth token successfully authorized and saved to: {token_path}")
        return flow.credentials

    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    print("\n" + "=" * 75, flush=True)
    print("👉 Open this URL in your browser to authorize read-only Gmail access:", flush=True)
    print(f"\n{auth_url}\n", flush=True)
    print("=" * 75 + "\n", flush=True)

    _OAuthCallbackHandler.auth_code = None
    _OAuthCallbackHandler.auth_error = None

    server = http.server.HTTPServer(("localhost", 8080), _OAuthCallbackHandler)
    server.timeout = 180

    if open_browser:
        webbrowser.open(auth_url)

    logger.info("Listening for OAuth callback on http://localhost:8080/ ...")
    while _OAuthCallbackHandler.auth_code is None and _OAuthCallbackHandler.auth_error is None:
        server.handle_request()

    server.server_close()

    if _OAuthCallbackHandler.auth_error:
        raise RuntimeError(f"OAuth authorization failed: {_OAuthCallbackHandler.auth_error}")

    if not _OAuthCallbackHandler.auth_code:
        raise RuntimeError("No authorization code received.")

    logger.info("Received authorization code. Exchanging for credentials...")
    flow.fetch_token(code=_OAuthCallbackHandler.auth_code)

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(flow.credentials.to_json())

    logger.success(f"Gmail OAuth token successfully authorized and saved to: {token_path}")
    return flow.credentials


def get_gmail_service(secrets_dir: Optional[Path] = None) -> Resource:
    """
    Load credentials from token_gmail.json and refresh them automatically if expired.
    Raises FileNotFoundError if token_gmail.json does not exist.
    """
    client_secret_path, token_path = get_gmail_secrets_paths(secrets_dir)

    if not token_path.is_file():
        msg = (
            f"Gmail OAuth token not found at: {token_path}\n"
            f"Run 'python claims_sync.py auth' to complete one-time read-only authorization."
        )
        logger.warning(msg)
        raise FileNotFoundError(msg)

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_READONLY_SCOPE)
    except Exception as e:
        msg = f"Failed to parse token_gmail.json: {e}. Run 'python claims_sync.py auth' to re-authenticate."
        logger.error(msg)
        raise RuntimeError(msg) from e

    if creds and creds.expired and creds.refresh_token:
        logger.info("Gmail OAuth access token expired; refreshing via refresh_token...")
        try:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            logger.info("Gmail OAuth token refreshed successfully.")
        except Exception as e:
            msg = f"Failed to refresh Gmail OAuth token: {e}. Run 'python claims_sync.py auth' to re-authenticate."
            logger.error(msg)
            raise RuntimeError(msg) from e

    if not creds or not creds.valid:
        msg = "Invalid Gmail credentials. Run 'python claims_sync.py auth' to re-authenticate."
        logger.error(msg)
        raise RuntimeError(msg)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)
