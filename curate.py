#!/usr/bin/env python3
"""
Root shim for Curation Agent CLI: delegates to agent/curate.py.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AGENT_DIR = BASE_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from curate import main

if __name__ == "__main__":
    main()
