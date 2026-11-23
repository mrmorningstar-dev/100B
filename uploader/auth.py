import os
from pathlib import Path
from typing import Optional
from loguru import logger

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from uploader.alerts import send_telegram_alert


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

MANAGE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
]

DEFAULT_SECRETS_DIR = Path(__file__).resolve().parent / "secrets"


def get_secrets_paths(secrets_dir: Optional[Path] = None, manage: bool = False) -> tuple[Path, Path]:
    """Return paths to (client_secret.json, token.json or token_manage.json)."""
    sdir = Path(secrets_dir or DEFAULT_SECRETS_DIR).resolve()
    sdir.mkdir(parents=True, exist_ok=True)
    token_filename = "token_manage.json" if manage else "token.json"
    return sdir / "client_secret.json", sdir / token_filename


def check_youtube_auth(secrets_dir: Optional[Path] = None, manage: bool = False) -> dict:
    """
    Check health of YouTube OAuth credentials without crashing or looping.
    Attempts token refresh if expired.
    Returns:
        {
            "status": "ok" | "expired" | "missing" | "error",
            "message": str
        }
    Never logs or exposes secret tokens.
    """
    client_secret_path, token_path = get_secrets_paths(secrets_dir, manage=manage)
    active_scopes = MANAGE_SCOPES if manage else SCOPES

    if not token_path.is_file():
        return {
            "status": "missing",
            "message": f"YouTube OAuth token file not found at {token_path.name}. Run 'python upload.py auth'."
        }

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), active_scopes)
    except Exception as e:
        return {
            "status": "expired",
            "message": f"Could not parse token file ({token_path.name}): {type(e).__name__}."
        }

    if not creds:
        return {
            "status": "expired",
            "message": "Credentials could not be initialized from token file."
        }

    # If access token is expired or needs refresh
    if creds.expired or not creds.valid:
        if not creds.refresh_token:
            return {
                "status": "expired",
                "message": "Token expired and no refresh token available. Re-authorization required."
            }
        try:
            creds.refresh(Request())
            # Save updated token
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            return {
                "status": "ok",
                "message": "Token refreshed successfully."
            }
        except Exception as e:
            err_str = str(e).lower()
            if "invalid_grant" in err_str or "revoked" in err_str or "expired" in err_str:
                return {
                    "status": "expired",
                    "message": "YouTube login expired or revoked (invalid_grant). Re-authorization required."
                }
            # Transient network, socket, or server error
            return {
                "status": "error",
                "message": f"Temporary network error refreshing token: {type(e).__name__}."
            }

    return {
        "status": "ok",
        "message": "Token is valid."
    }


def authenticate_interactive(secrets_dir: Optional[Path] = None, manage: bool = False) -> Credentials:
    """
    Run one-time interactive OAuth2 authorization in the user's browser.
    Saves token.json or token_manage.json (including refresh token) upon completion.
    """
    client_secret_path, token_path = get_secrets_paths(secrets_dir, manage=manage)
    active_scopes = MANAGE_SCOPES if manage else SCOPES

    if not client_secret_path.is_file():
        # Check if there is any client_secret*.json file in secrets dir
        candidates = list(client_secret_path.parent.glob("client_secret*.json"))
        if candidates:
            client_secret_path = candidates[0]
        else:
            err = (
                f"OAuth client secret file not found at: {client_secret_path}\n"
                f"Please download your OAuth 2.0 Client ID (Desktop Application) from the Google Cloud Console, "
                f"rename it to 'client_secret.json', and place it in '{client_secret_path.parent}'."
            )
            logger.error(err)
            raise FileNotFoundError(err)

    logger.info(f"Initiating interactive OAuth flow (manage={manage}) using: {client_secret_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), active_scopes)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    logger.success(f"OAuth credentials successfully authorized and saved to: {token_path}")
    return creds


def get_authenticated_service(secrets_dir: Optional[Path] = None) -> Resource:
    """
    Load credentials from token.json and refresh them automatically if expired.
    If refresh fails (token revoked or expired), halts immediately, fires a Telegram alert,
    and never loops on auth errors.
    """
    client_secret_path, token_path = get_secrets_paths(secrets_dir)

    if not token_path.is_file():
        msg = (
            f"YouTube OAuth token not found at: {token_path}\n"
            f"Run 'python upload.py auth' to complete the one-time interactive authorization."
        )
        logger.error(msg)
        send_telegram_alert("AUTH_MISSING", msg)
        raise FileNotFoundError(msg)

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as e:
        msg = f"Failed to parse token file: {e}. Run 'python upload.py auth' to re-authenticate."
        logger.error(msg)
        send_telegram_alert("AUTH_CORRUPT", msg)
        raise RuntimeError(msg) from e

    if creds and creds.expired and creds.refresh_token:
        logger.info("YouTube OAuth access token expired; refreshing via refresh_token...")
        try:
            creds.refresh(Request())
            # Save updated token
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            logger.info("OAuth token refreshed successfully.")
        except Exception as e:
            msg = f"OAuth token refresh failed (revoked or expired): {e}. Manual re-auth required: 'python upload.py auth'"
            logger.critical(msg)
            send_telegram_alert("AUTH_REVOKED", msg, {"error": str(e)})
            raise RuntimeError(msg) from e

    if not creds or not creds.valid:
        msg = "OAuth credentials are invalid. Please run 'python upload.py auth'."
        logger.critical(msg)
        send_telegram_alert("AUTH_INVALID", msg)
        raise RuntimeError(msg)

    return build("youtube", "v3", credentials=creds, cache_discovery=False)
