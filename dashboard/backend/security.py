import os
import time
import hmac
import hashlib
import secrets
import logging
from typing import Any, Dict, List, Set
from fastapi import Request, HTTPException, status

logger = logging.getLogger("dashboard.security")

# Failed authentication tracking for rate limiting
# client_ip -> list of failure timestamps
_FAILED_AUTH_LOG: Dict[str, List[float]] = {}
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_WINDOW_SECONDS = 60.0

LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
LOOPBACK_CLIENT_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_allowed_hosts() -> Set[str]:
    """Return set of allowed lowercase hostnames from DASHBOARD_ALLOWED_HOSTS or defaults."""
    raw = os.getenv("DASHBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def is_host_allowed(request: Request) -> bool:
    """
    Check if incoming request Host is allowed.
    Only the Host header counts (X-Forwarded-Host is not used).
    Strips port numbers before comparing.
    If DASHBOARD_API_KEY is empty, only localhost/127.0.0.1/testserver are allowed.
    """
    val = request.headers.get("host", "")
    if not val:
        return False
    host_clean = val.split(":")[0].strip().lower()

    configured_key = get_configured_api_key()
    if not configured_key:
        return host_clean in LOCAL_HOSTS

    allowed = get_allowed_hosts()
    return host_clean in allowed


def verify_request_host(request: Request) -> None:
    """Raise 403 Forbidden if request host is not allowed."""
    val = request.headers.get("host", "")
    host_clean = val.split(":")[0].strip().lower() if val else ""

    configured_key = get_configured_api_key()
    if not configured_key:
        if host_clean not in LOCAL_HOSTS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Remote host '{host_clean}' refused because DASHBOARD_API_KEY is not configured.",
            )
        return

    allowed = get_allowed_hosts()
    if host_clean not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Host '{host_clean}' is not in DASHBOARD_ALLOWED_HOSTS: {allowed}",
        )


def get_client_ip(request: Request) -> str:
    """
    Get client IP for rate limiting.
    When request.client.host is a loopback address (e.g. behind Cloudflare tunnel),
    use CF-Connecting-IP header as the client id if present.
    """
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip in LOOPBACK_CLIENT_IPS:
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
    return direct_ip


def check_rate_limit(client_ip: str) -> bool:
    """Return True if client is within allowed failure limits, False if rate-limited."""
    now = time.time()
    attempts = _FAILED_AUTH_LOG.get(client_ip, [])
    # Filter attempts within window
    valid = [t for t in attempts if now - t < LOCKOUT_WINDOW_SECONDS]
    _FAILED_AUTH_LOG[client_ip] = valid
    return len(valid) < MAX_FAILED_ATTEMPTS


def record_failed_attempt(client_ip: str) -> None:
    """Record a failed auth attempt timestamp for rate limiting."""
    now = time.time()
    attempts = _FAILED_AUTH_LOG.setdefault(client_ip, [])
    attempts.append(now)


def get_configured_api_key() -> str:
    """Return DASHBOARD_API_KEY from environment or empty string if disabled."""
    return os.getenv("DASHBOARD_API_KEY", "").strip()


def verify_api_token(request: Request) -> bool:
    """
    Verify X-Dashboard-Token header against DASHBOARD_API_KEY with constant-time comparison.
    If DASHBOARD_API_KEY is not configured, authentication is bypassed for local hosts.
    Rate limits failed authentication attempts by client IP (using CF-Connecting-IP for proxies).
    """
    # OPTIONS requests are preflight checks and skip auth
    if request.method == "OPTIONS":
        return True

    configured_key = get_configured_api_key()
    if not configured_key:
        return True

    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts. Please try again later.",
        )

    # Check X-Dashboard-Token header, then fallback to Authorization: Bearer
    token = request.headers.get("x-dashboard-token", "").strip()
    if not token:
        auth_header = request.headers.get("authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token or not secrets.compare_digest(token, configured_key):
        record_failed_attempt(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid X-Dashboard-Token.",
        )
    return True


def generate_media_token(validity_seconds: int = 600) -> Dict[str, Any]:
    """Generate a short-lived signed media token valid for thumbnail and video requests."""
    configured_key = get_configured_api_key()
    secret = configured_key if configured_key else "local-dashboard-media-fallback-secret"
    expires_at = int(time.time()) + validity_seconds
    msg = f"media:{expires_at}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "token": f"{expires_at}.{sig}",
        "expires_at": expires_at,
        "validity_seconds": validity_seconds,
    }


def verify_media_access(request: Request) -> bool:
    """
    Verify access for media requests (thumbnails and video streaming).
    Accepts:
      1. Valid short-lived signed ?token= query parameter (HMAC)
      2. Valid X-Dashboard-Token header
      3. Bypassed if DASHBOARD_API_KEY is not configured
    """
    configured_key = get_configured_api_key()
    if not configured_key:
        return True

    # 1. Check if valid X-Dashboard-Token header is present
    token_hdr = request.headers.get("x-dashboard-token", "").strip()
    if token_hdr and secrets.compare_digest(token_hdr, configured_key):
        return True

    # 2. Check signed media token in query parameter (?token=expires_at.signature)
    media_token = request.query_params.get("token", "").strip()
    if not media_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Media request requires a valid signed token or X-Dashboard-Token.",
        )

    try:
        exp_str, sig = media_token.split(".", 1)
        expires_at = int(exp_str)
        if time.time() > expires_at:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Signed media token has expired.",
            )
        msg = f"media:{expires_at}".encode("utf-8")
        expected_sig = hmac.new(configured_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected_sig):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Invalid signed media token signature.",
            )
        return True
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Media token verification error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Malformed media token.",
        )
