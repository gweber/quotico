"""
backend/app/services/team_alias_normalizer.py

Purpose:
    Normalize team alias strings for robust matching across providers and user
    input variants.

Dependencies:
    - re
    - unicodedata
"""

from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_SPACE_RE = re.compile(r"\s+")
_TRANSLIT_MAP = {
    "ae": "a",
    "oe": "o",
    "ue": "u",
    "ss": "ss",
}


def normalize_team_alias(raw: str) -> str:
    """
    Normalize alias text into an ASCII-safe key.

    Steps:
        1. lowercase + trim
        2. NFKD accent removal
        3. punctuation cleanup
        4. whitespace collapse
        5. compatibility transliteration (e.g. muenchen -> munchen)
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        return ""

    tokens: list[str] = []
    for token in text.split(" "):
        normalized = token
        for src, dst in _TRANSLIT_MAP.items():
            normalized = normalized.replace(src, dst)
        if normalized:
            tokens.append(normalized)
    return _SPACE_RE.sub(" ", " ".join(tokens)).strip()

