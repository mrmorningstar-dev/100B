"""
Root conftest.py: Enforces strict isolation for all test runs.
Guarantees tests NEVER touch the real production database (agent/data/agent.db)
or production alert logs (uploader/logs/alerts.log, agent/logs/rights_alerts.log).
"""

import os
import sys
import shutil
import tempfile
import pytest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
AGENT_DIR = WORKSPACE_ROOT / "agent"
PROD_DB_PATH = (WORKSPACE_ROOT / "agent" / "data" / "agent.db").resolve()
PROD_ALERTS_LOG_PATH = (WORKSPACE_ROOT / "uploader" / "logs" / "alerts.log").resolve()
PROD_RIGHTS_ALERTS_LOG_PATH = (WORKSPACE_ROOT / "agent" / "logs" / "rights_alerts.log").resolve()

for p in [str(WORKSPACE_ROOT), str(AGENT_DIR)]:
    if p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(AGENT_DIR))

# Clear any cached 'core' package from scraper so agent.core is loaded by default
if "core" in sys.modules and not hasattr(sys.modules["core"], "db"):
    for key in list(sys.modules.keys()):
        if key == "core" or key.startswith("core."):
            del sys.modules[key]


@pytest.fixture(autouse=True, scope="session")
def isolate_production_database(tmp_path_factory):
    """
    Session-level safety fixture:
    Sets AGENT_DB_PATH, ALERTS_LOG_PATH, and RIGHTS_ALERTS_LOG_PATH to isolated temporary
    files for any code that queries them, and sets PYTEST_RUNNING to trigger hard redirect guards.
    """
    temp_dir = tmp_path_factory.mktemp("global_test_db")
    temp_db_file = temp_dir / "isolated_session_agent.db"
    temp_alerts_log = temp_dir / "test_alerts.log"
    temp_rights_log = temp_dir / "test_rights_alerts.log"

    # Initialize schema in session test database
    from agent.core.db import AgentDatabase
    AgentDatabase(temp_db_file)

    old_agent_db_path = os.environ.get("AGENT_DB_PATH")
    old_alerts_log_path = os.environ.get("ALERTS_LOG_PATH")
    old_rights_log_path = os.environ.get("RIGHTS_ALERTS_LOG_PATH")

    os.environ["AGENT_DB_PATH"] = str(temp_db_file)
    os.environ["ALERTS_LOG_PATH"] = str(temp_alerts_log)
    os.environ["RIGHTS_ALERTS_LOG_PATH"] = str(temp_rights_log)
    os.environ["PYTEST_RUNNING"] = "1"

    if "uploader.alerts" in sys.modules:
        sys.modules["uploader.alerts"].ALERTS_LOG_PATH = temp_alerts_log
    if "agent.core.rights.alerts" in sys.modules:
        sys.modules["agent.core.rights.alerts"].LOG_FILE = temp_rights_log

    yield temp_db_file

    # Cleanup
    if old_agent_db_path is not None:
        os.environ["AGENT_DB_PATH"] = old_agent_db_path
    else:
        os.environ.pop("AGENT_DB_PATH", None)

    if old_alerts_log_path is not None:
        os.environ["ALERTS_LOG_PATH"] = old_alerts_log_path
    else:
        os.environ.pop("ALERTS_LOG_PATH", None)

    if old_rights_log_path is not None:
        os.environ["RIGHTS_ALERTS_LOG_PATH"] = old_rights_log_path
    else:
        os.environ.pop("RIGHTS_ALERTS_LOG_PATH", None)

    os.environ.pop("PYTEST_RUNNING", None)


@pytest.fixture(autouse=True)
def guard_against_production_db_access():
    """
    Per-test safety guard:
    Records production agent.db and alert logs mtime/size before and after each test
    to guarantee zero writes leak to production.
    """
    prod_stat_before = None
    if PROD_DB_PATH.exists():
        stat = PROD_DB_PATH.stat()
        prod_stat_before = (stat.st_mtime_ns, stat.st_size)

    alerts_stat_before = None
    if PROD_ALERTS_LOG_PATH.exists():
        stat = PROD_ALERTS_LOG_PATH.stat()
        alerts_stat_before = (stat.st_mtime_ns, stat.st_size)

    rights_stat_before = None
    if PROD_RIGHTS_ALERTS_LOG_PATH.exists():
        stat = PROD_RIGHTS_ALERTS_LOG_PATH.stat()
        rights_stat_before = (stat.st_mtime_ns, stat.st_size)

    yield

    if prod_stat_before and PROD_DB_PATH.exists():
        stat = PROD_DB_PATH.stat()
        prod_stat_after = (stat.st_mtime_ns, stat.st_size)
        assert prod_stat_before == prod_stat_after, (
            f"SAFETY VIOLATION: Test modified production database at {PROD_DB_PATH}! "
            "Tests must strictly use a temporary database."
        )

    if alerts_stat_before and PROD_ALERTS_LOG_PATH.exists():
        stat = PROD_ALERTS_LOG_PATH.stat()
        alerts_stat_after = (stat.st_mtime_ns, stat.st_size)
        assert alerts_stat_before == alerts_stat_after, (
            f"SAFETY VIOLATION: Test modified production alerts log at {PROD_ALERTS_LOG_PATH}! "
            "Tests must strictly use a temporary log file."
        )

    if rights_stat_before and PROD_RIGHTS_ALERTS_LOG_PATH.exists():
        stat = PROD_RIGHTS_ALERTS_LOG_PATH.stat()
        rights_stat_after = (stat.st_mtime_ns, stat.st_size)
        assert rights_stat_before == rights_stat_after, (
            f"SAFETY VIOLATION: Test modified production rights alerts log at {PROD_RIGHTS_ALERTS_LOG_PATH}! "
            "Tests must strictly use a temporary log file."
        )


@pytest.fixture(autouse=True)
def block_live_reddit_requests(request, monkeypatch):
    """
    Autouse safety fixture that blocks all outbound HTTP network requests
    to reddit.com during test runs unless explicitly marked with @pytest.mark.live.
    """
    if "live" in request.keywords:
        return

    # Intercept httpx
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

    # Intercept urllib.request
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
