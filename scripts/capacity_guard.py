#!/usr/bin/env python3
"""Run directly from a checkout or a Codex plugin cache, without installing dependencies."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity_guard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
