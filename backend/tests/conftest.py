"""
backend/tests/conftest.py

Purpose:
    Shared pytest bootstrap for import paths used by backend and root-level
    tool module tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_BACKEND_DIR = _THIS_FILE.parents[1]
_REPO_ROOT = _THIS_FILE.parents[2]

for candidate in (str(_BACKEND_DIR), str(_REPO_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

