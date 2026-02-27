"""
backend/tests/test_league_needs_review_removed.py

Purpose:
    Static regression guard: league-domain runtime files must not reference
    `needs_review` after the hard-cut removal.
"""

from __future__ import annotations

from pathlib import Path


TARGET_FILES = [
    "backend/app/services/sportmonks_connector.py",
    "backend/app/services/league_service.py",
    "backend/app/routers/admin.py",
    "backend/app/routers/admin_ingest.py",
    "backend/app/models/leagues.py",
]


def test_league_domain_has_no_needs_review_references() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    offending: list[str] = []
    for rel in TARGET_FILES:
        path = repo_root / rel
        raw = path.read_text(encoding="utf-8")
        if "needs_review" in raw:
            offending.append(rel)
    assert not offending, f"`needs_review` found in league-domain files: {offending}"
