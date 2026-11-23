import os
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from tools.editor_tool import EditorTool
from tools.scraper_tool import ScraperTool


def test_editor_tool_subprocess_call(tmp_path):
    """Verify EditorTool invokes subprocess.run with PYTHONUTF8=1 without NameError."""
    dummy_input = tmp_path / "test_input.mp4"
    dummy_input.write_bytes(b"dummy video data")
    dummy_output = tmp_path / "test_output.mp4"

    tool = EditorTool(editor_script=tmp_path / "main.py")

    with patch("subprocess.run") as mock_run:
        # Simulate successful subprocess run and output file creation
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Rendered branded video successfully"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        # Create output file to satisfy exists() and >= 50KB check
        dummy_output.write_bytes(b"x" * 50_000)

        success, msg = tool.edit_single_video(
            input_video_path=dummy_input,
            caption="Check this out 😂🔥",
            output_video_path=dummy_output,
            dry_run=False,
        )

        assert success is True
        assert "Successfully edited" in msg
        mock_run.assert_called_once()

        # Check call arguments and environment
        call_kwargs = mock_run.call_args[1]
        env_passed = call_kwargs.get("env", {})
        assert env_passed.get("PYTHONUTF8") == "1"
        assert env_passed.get("PYTHONIOENCODING") == "utf-8"
        assert call_kwargs.get("encoding") == "utf-8"


def test_scraper_tool_subprocess_call(tmp_path):
    """Verify ScraperTool invokes subprocess.run with PYTHONUTF8=1 without NameError."""
    tool = ScraperTool(scraper_dir=tmp_path)

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Scraped 5 candidates"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        success = tool.run_scrape(
            target_count=5,
            date_str="2026-11-12",
            sources=["reddit"],
            subreddits=["funny", "Unexpected"],
            dry_run=False,
        )

        assert success is True
        mock_run.assert_called_once()

        # Check call arguments and environment
        call_kwargs = mock_run.call_args[1]
        env_passed = call_kwargs.get("env", {})
        assert env_passed.get("PYTHONUTF8") == "1"
        assert env_passed.get("PYTHONIOENCODING") == "utf-8"
        assert call_kwargs.get("encoding") == "utf-8"


def test_scraper_tool_crash_sets_scraper_error(tmp_path):
    """Verify ScraperTool flags error when subprocess exits non-zero without being blocked."""
    tool = ScraperTool(scraper_dir=tmp_path)

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # Process crashed / unhandled failure
        mock_proc.stdout = ""
        mock_proc.stderr = "Traceback (most recent call last): Exception: fatal crash"
        mock_run.return_value = mock_proc

        cands = tool.list_candidates(
            target_count=10,
            date_str="2026-11-23",
            sources=["reddit"],
        )

        assert cands == []
        assert tool.has_scraper_error() is True
        assert tool.is_scraper_blocked() is False
        assert tool.last_scrape_status == "error"


def test_scraper_tool_blocked_not_marked_as_crash(tmp_path):
    """Verify ScraperTool preserves 'blocked' status and does not flag generic crash when circuit breaker tripped."""
    tool = ScraperTool(scraper_dir=tmp_path)

    # Prepare scrape_result.json with status="blocked"
    cands_dir = tmp_path / "data" / "candidates"
    cands_dir.mkdir(parents=True, exist_ok=True)
    res_file = cands_dir / "2026-11-23_scrape_result.json"
    res_file.write_text(json.dumps({"status": "blocked", "blocked_until": "11:07 AM IST"}), encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # polite exit after breaker trip
        mock_proc.stdout = "Circuit breaker tripped"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        cands = tool.list_candidates(
            target_count=10,
            date_str="2026-11-23",
            sources=["reddit"],
        )

        assert cands == []
        assert tool.is_scraper_blocked() is True
        assert tool.has_scraper_error() is False
        assert tool.last_scrape_status == "blocked"


def test_scraper_tool_exception_sets_scraper_error(tmp_path):
    """Verify ScraperTool flags error when subprocess raises an exception."""
    tool = ScraperTool(scraper_dir=tmp_path)

    with patch("subprocess.run", side_effect=RuntimeError("Subprocess failed to launch")):
        cands = tool.list_candidates(
            target_count=10,
            date_str="2026-11-23",
            sources=["reddit"],
        )

        assert cands == []
        assert tool.has_scraper_error() is True
        assert tool.is_scraper_blocked() is False
        assert tool.last_scrape_status == "error"
