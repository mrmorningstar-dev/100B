import os
import sys
import tempfile
import pytest
from pathlib import Path

agent_root = Path(__file__).parent.parent.resolve()

if str(agent_root) in sys.path:
    sys.path.remove(str(agent_root))
sys.path.insert(0, str(agent_root))

# Clear any previously cached 'core' package from scraper or root
if "core" in sys.modules and not hasattr(sys.modules["core"], "frames"):
    for key in list(sys.modules.keys()):
        if key == "core" or key.startswith("core."):
            del sys.modules[key]

# Ensure alert logs never write to real files even when pytest is invoked from agent/
if "ALERTS_LOG_PATH" not in os.environ:
    os.environ["ALERTS_LOG_PATH"] = str(Path(tempfile.gettempdir()) / "test_alerts.log")
if "RIGHTS_ALERTS_LOG_PATH" not in os.environ:
    os.environ["RIGHTS_ALERTS_LOG_PATH"] = str(Path(tempfile.gettempdir()) / "test_rights_alerts.log")
os.environ["PYTEST_RUNNING"] = "1"


@pytest.fixture(autouse=True)
def block_live_reddit_requests(request, monkeypatch):
    """
    Autouse safety fixture that blocks all outbound HTTP network requests
    to reddit.com during test runs unless explicitly marked with @pytest.mark.live.
    """
    if "live" in request.keywords:
        return

    try:
        import httpx
        orig_httpx_send = httpx.Client.send

        def guarded_httpx_send(client_self, req, *args, **kwargs):
            host = str(getattr(req, "url", "")).lower()
            if "reddit.com" in host or "redd.it" in host:
                raise RuntimeError(
                    f"TEST SAFETY GUARD: Outbound network request to Reddit blocked during test execution ({req.url}). "
                    "Tests must mock Reddit or be marked with @pytest.mark.live."
                )
            return orig_httpx_send(client_self, req, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "send", guarded_httpx_send)
    except ImportError:
        pass

    try:
        import urllib.request
        orig_urlopen = urllib.request.urlopen

        def guarded_urlopen(req, *args, **kwargs):
            url_str = req.full_url if hasattr(req, "full_url") else str(req)
            if "reddit.com" in url_str.lower() or "redd.it" in url_str.lower():
                raise RuntimeError(
                    f"TEST SAFETY GUARD: Outbound urllib request to Reddit blocked during test execution ({url_str}). "
                    "Tests must mock Reddit or be marked with @pytest.mark.live."
                )
            return orig_urlopen(req, *args, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    except Exception:
        pass
