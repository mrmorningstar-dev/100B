import os
import pytest
from starlette.testclient import TestClient
from dashboard.backend.app import create_app
import dashboard.backend.security as sec


def test_host_allowlist(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "test-api-key")
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,my-tunnel.cfargotunnel.com")
    app = create_app()
    client = TestClient(app)

    # Allowed host
    res_ok = client.get("/api/health", headers={"host": "my-tunnel.cfargotunnel.com"})
    assert res_ok.status_code == 200

    # Unallowed host
    res_bad = client.get("/api/health", headers={"host": "malicious.com"})
    assert res_bad.status_code == 403
    assert "malicious.com" in res_bad.json()["error"]


def test_x_forwarded_host_ignored(monkeypatch):
    """Ensure X-Forwarded-Host is ignored; only the Host header counts."""
    monkeypatch.setenv("DASHBOARD_API_KEY", "test-api-key")
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,my-tunnel.cfargotunnel.com")
    app = create_app()
    client = TestClient(app)

    # Host is malicious, but X-Forwarded-Host is allowed tunnel -> must be 403 Forbidden
    res = client.get(
        "/api/health",
        headers={"host": "malicious.com", "x-forwarded-host": "my-tunnel.cfargotunnel.com"},
    )
    assert res.status_code == 403
    assert "malicious.com" in res.json()["error"]


def test_empty_api_key_refuses_remote_host(monkeypatch):
    """If DASHBOARD_API_KEY is empty, refuse requests whose Host is not 127.0.0.1/localhost."""
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,my-tunnel.cfargotunnel.com")
    app = create_app()
    client = TestClient(app)

    # Localhost / testserver -> 200
    res_local = client.get("/api/health", headers={"host": "testserver"})
    assert res_local.status_code == 200

    # Remote tunnel host when DASHBOARD_API_KEY is empty -> 403 Forbidden with clear message
    res_remote = client.get("/api/health", headers={"host": "my-tunnel.cfargotunnel.com"})
    assert res_remote.status_code == 403
    assert "DASHBOARD_API_KEY is not configured" in res_remote.json()["error"]


def test_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    app = create_app()
    client = TestClient(app)

    res = client.get("/api/pipeline/status")
    # Should not be 401 when DASHBOARD_API_KEY is not set
    assert res.status_code != 401


def test_auth_enforced_when_configured(monkeypatch):
    test_key = "super-secret-key-12345"
    monkeypatch.setenv("DASHBOARD_API_KEY", test_key)
    sec._FAILED_AUTH_LOG.clear()
    app = create_app()
    client = TestClient(app)

    # Missing token -> 401
    res_missing = client.get("/api/pipeline/status")
    assert res_missing.status_code == 401
    assert "Missing or invalid X-Dashboard-Token" in res_missing.json()["error"]

    # Wrong token -> 401
    res_wrong = client.get("/api/pipeline/status", headers={"x-dashboard-token": "wrong-key"})
    assert res_wrong.status_code == 401

    # Valid token -> not 401
    res_valid = client.get("/api/pipeline/status", headers={"x-dashboard-token": test_key})
    assert res_valid.status_code != 401

    # Bearer token format -> not 401
    res_bearer = client.get("/api/pipeline/status", headers={"authorization": f"Bearer {test_key}"})
    assert res_bearer.status_code != 401


def test_rate_limit_cf_connecting_ip(monkeypatch):
    """When client is loopback/testclient, use CF-Connecting-IP header for rate limiting."""
    test_key = "super-secret-key-12345"
    monkeypatch.setenv("DASHBOARD_API_KEY", test_key)
    sec._FAILED_AUTH_LOG.clear()
    app = create_app()
    client = TestClient(app)

    cf_attacker_ip = "203.0.113.50"

    # 10 failed attempts from this CF IP
    for _ in range(10):
        res = client.get(
            "/api/pipeline/status",
            headers={"x-dashboard-token": "bad-token", "cf-connecting-ip": cf_attacker_ip},
        )
        assert res.status_code == 401

    # 11th attempt from same CF IP -> 429 Too Many Requests
    res_429 = client.get(
        "/api/pipeline/status",
        headers={"x-dashboard-token": "bad-token", "cf-connecting-ip": cf_attacker_ip},
    )
    assert res_429.status_code == 429
    assert "Too many failed authentication attempts" in res_429.json()["error"]

    # Different CF IP is NOT rate-limited
    res_other = client.get(
        "/api/pipeline/status",
        headers={"x-dashboard-token": test_key, "cf-connecting-ip": "198.51.100.99"},
    )
    assert res_other.status_code == 200


def test_options_preflight_skips_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "super-secret-key-12345")
    monkeypatch.setenv("DASHBOARD_CORS_ORIGINS", "https://my-dashboard.vercel.app")
    app = create_app()
    client = TestClient(app)

    # Preflight OPTIONS request
    res = client.options(
        "/api/schedule",
        headers={
            "Origin": "https://my-dashboard.vercel.app",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-dashboard-request,x-dashboard-token,content-type",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://my-dashboard.vercel.app"


def test_cors_headers_on_401(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "super-secret-key-12345")
    monkeypatch.setenv("DASHBOARD_CORS_ORIGINS", "https://my-dashboard.vercel.app")
    app = create_app()
    client = TestClient(app)

    # Unauthorized request from Vercel origin must still have CORS headers
    res = client.get("/api/pipeline/status", headers={"Origin": "https://my-dashboard.vercel.app"})
    assert res.status_code == 401
    assert res.headers.get("access-control-allow-origin") == "https://my-dashboard.vercel.app"
    assert "Content-Range" in res.headers.get("access-control-expose-headers", "")


def test_media_token_generation_and_verification(monkeypatch):
    test_key = "media-secret-token"
    monkeypatch.setenv("DASHBOARD_API_KEY", test_key)
    app = create_app()
    client = TestClient(app)

    # 1. Request signed media token with API key
    res_token = client.get("/api/auth/media-token", headers={"x-dashboard-token": test_key})
    assert res_token.status_code == 200
    token_data = res_token.json()
    assert "token" in token_data
    media_token = token_data["token"]

    # 2. Check verify_media_access directly
    class DummyRequest:
        headers = {}
        query_params = {"token": media_token}
    assert sec.verify_media_access(DummyRequest()) is True

    # 3. Invalid token fails
    class BadTokenRequest:
        headers = {}
        query_params = {"token": "invalid.signature"}
    with pytest.raises(sec.HTTPException) as exc:
        sec.verify_media_access(BadTokenRequest())
    assert exc.value.status_code == 401


def test_media_token_expiry(monkeypatch):
    """Test that an expired media token is rejected with 401."""
    test_key = "media-secret-token"
    monkeypatch.setenv("DASHBOARD_API_KEY", test_key)

    # Generate a token with negative validity (expired in the past)
    expired_token_data = sec.generate_media_token(validity_seconds=-10)

    class ExpiredRequest:
        headers = {}
        query_params = {"token": expired_token_data["token"]}

    with pytest.raises(sec.HTTPException) as exc:
        sec.verify_media_access(ExpiredRequest())
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()
