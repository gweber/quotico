"""
backend/app/utils/team_matching.py

Purpose:
    Shared fuzzy team-name matching helpers used by provider adapters and Team
    Tower conflict checks. The heuristics are intentionally simple and
    deterministic to support repeatable ingest behavior.

Notes:
    - External IDs always take precedence over fuzzy names.
    - Fuzzy matching is a fallback only and can produce edge-case false
      positives/negatives; callers must keep conflict handling explicit.
"""

from __future__ import annotations

import unicodedata


def _normalize_tokens(name: str) -> set[str]:
    """Normalize a team name into lowercase, accent-free comparison tokens."""
    normalized = unicodedata.normalize("NFKD", name or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()
    noise = {"fc", "cf", "sc", "ac", "as", "ss", "us", "afc", "rcd", "1.", "club", "de"}
    return {token for token in normalized.split() if token not in noise and len(token) >= 3}


def teams_match(name_a: str, name_b: str) -> bool:
    """Return True when both names likely refer to the same team."""
    tokens_a = _normalize_tokens(name_a)
    tokens_b = _normalize_tokens(name_b)
    if not tokens_a or not tokens_b:
        return False

    if tokens_a & tokens_b:
        return True

    for token_a in tokens_a:
        for token_b in tokens_b:
            if len(token_a) >= 4 and len(token_b) >= 4:
                if token_a.startswith(token_b[:4]) or token_b.startswith(token_a[:4]):
                    return True
    return False

