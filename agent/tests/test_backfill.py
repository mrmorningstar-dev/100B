import tempfile
from pathlib import Path
import pytest
from unittest.mock import MagicMock
from graph.nodes.backfill import backfill_node


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


def test_backfill_no_shortage(mock_db):
    state = {
        "target_count": 2,
        "approved": [{"post_id": "vid1"}, {"post_id": "vid2"}],
        "backfill_performed": False,
    }
    result = backfill_node(state, mock_db)
    assert result["backfill_performed"] is True
    assert result["trigger_extra_loop"] is False
    assert len(result["approved"]) == 2
    mock_db.fetch_reserve_pool_candidates.assert_not_called()


def test_backfill_sufficient_reserves(mock_db):
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        f1 = tmp_dir / "res1.mp4"
        f2 = tmp_dir / "res2.mp4"
        f1.write_bytes(b"video1")
        f2.write_bytes(b"video2")

        mock_db.fetch_reserve_pool_candidates.return_value = [
            {"post_id": "res1", "title": "Reserve Clip 1", "video_path": str(f1)},
            {"post_id": "res2", "title": "Reserve Clip 2", "video_path": str(f2)},
        ]
        state = {
            "target_count": 3,
            "approved": [{"post_id": "vid1"}],
            "backfill_performed": False,
        }
        result = backfill_node(state, mock_db)
        assert result["backfill_performed"] is True
        assert result["trigger_extra_loop"] is False
        assert len(result["approved"]) == 3
        assert result["approved"][1]["status"] == "reserve_backfill"
        assert result["approved"][1]["is_reserve_backfill"] is True
        assert result["approved"][1]["already_edited"] is False
        assert result["approved"][1]["video_path"] == str(f1)
        mock_db.fetch_reserve_pool_candidates.assert_called_once_with(limit=20)


def test_backfill_insufficient_reserves_triggers_extra_loop(mock_db):
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        f1 = tmp_dir / "res1.mp4"
        f1.write_bytes(b"video1")

        mock_db.fetch_reserve_pool_candidates.return_value = [
            {"post_id": "res1", "title": "Reserve Clip 1", "video_path": str(f1)}
        ]
        state = {
            "target_count": 4,
            "approved": [{"post_id": "vid1"}],
            "backfill_performed": False,
        }
        result = backfill_node(state, mock_db)
        assert result["backfill_performed"] is True
        assert result["trigger_extra_loop"] is True
        assert len(result["approved"]) == 2
        mock_db.fetch_reserve_pool_candidates.assert_called_once_with(limit=20)


def test_backfill_skips_missing_reserve_file(mock_db):
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        f_valid = tmp_dir / "valid.mp4"
        f_valid.write_bytes(b"valid_content")
        missing_path = tmp_dir / "deleted_file.mp4"  # Does not exist on disk

        mock_db.fetch_reserve_pool_candidates.return_value = [
            {"post_id": "res_missing", "title": "Missing File Clip", "video_path": str(missing_path)},
            {"post_id": "res_valid", "title": "Valid File Clip", "video_path": str(f_valid)},
        ]
        state = {
            "target_count": 2,
            "approved": [{"post_id": "vid1"}],
            "backfill_performed": False,
        }
        result = backfill_node(state, mock_db)

        # Missing file was skipped and marked missing
        mock_db.mark_reserve_missing.assert_called_once_with(["res_missing"])
        # Only valid file was selected (marked used transactionally upon schedule save)
        assert len(result["approved"]) == 2
        assert result["approved"][1]["post_id"] == "res_valid"
        assert result["trigger_extra_loop"] is False


def test_backfill_already_performed_no_op(mock_db):
    state = {
        "target_count": 4,
        "approved": [{"post_id": "vid1"}],
        "backfill_performed": True,
    }
    result = backfill_node(state, mock_db)
    assert result["trigger_extra_loop"] is False
    mock_db.fetch_reserve_pool_candidates.assert_not_called()


def test_backfill_human_interrupted(mock_db):
    state = {
        "target_count": 4,
        "approved": [],
        "human_interrupted": True,
        "backfill_performed": False,
    }
    result = backfill_node(state, mock_db)
    assert result == {}
    mock_db.fetch_reserve_pool_candidates.assert_not_called()


