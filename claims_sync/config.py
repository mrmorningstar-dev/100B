"""
claims_sync/config.py

Configuration management for Phase 6.1 Claims Sync & Auto-Action Policy.
Persisted in claims_sync/claims_config.json with safe defaults.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger

CONFIG_FILE_PATH = Path(__file__).resolve().parent / "claims_config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "auto_action_scheduled": "delete",  # 'delete', 'private', 'none'
    "auto_action_public": "delete",     # 'delete', 'private', 'none' (active only when dry_run=False)
    "dry_run": True,                    # True for first 3 days: simulate deletions & log planned actions
    "max_auto_actions_per_day": 6,      # Cap per calendar day
    "grace_hours": 0,                   # Delay before auto-action on public videos
    "keep_if_views_over": None,         # Off by default; if set, skip deletion of high-traffic videos
}


def load_claims_config() -> Dict[str, Any]:
    """Load configuration from disk or return defaults if not found."""
    if not CONFIG_FILE_PATH.is_file():
        save_claims_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            return merged
    except Exception as e:
        logger.warning(f"Failed to read claims_config.json: {e}; using defaults.")
        return dict(DEFAULT_CONFIG)


def save_claims_config(config: Dict[str, Any]) -> None:
    """Save configuration to disk."""
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write claims_config.json: {e}")


def set_dry_run(enabled: bool) -> Dict[str, Any]:
    """Update dry_run mode on or off."""
    cfg = load_claims_config()
    cfg["dry_run"] = bool(enabled)
    save_claims_config(cfg)
    logger.info(f"Claims sync dry_run mode set to: {cfg['dry_run']}")
    return cfg
