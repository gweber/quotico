"""
backend/app/services/justice_service.py

Purpose:
    Central justice-table engine for v3 analysis endpoints.
    Provides deterministic Poisson Monte Carlo expected-points calculations
    and season aggregation for "Unjust Table" views.

Dependencies:
    - app.database
    - app.utils
    - numpy
"""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from datetime import timedelta
from typing import Any

import numpy as np

import app.database as _db
from app.config import settings
from app.utils import ensure_utc, utcnow

_SIMULATIONS = 10_000
_CACHE_TTL = timedelta(seconds=int(settings.JUSTICE_CACHE_TTL_SECONDS))
_CACHE_VERSION = "v3.1"


class JusticeService:
    """Business logic for Monte Carlo expected-points table analysis."""

    _cache: dict[str, tuple[Any, dict[str, Any]]] = {}

    @staticmethod
    def _cache_get(key: str) -> dict[str, Any] | None:
        row = JusticeService._cache.get(key)
        if not row:
            return None
        expires_at, data = row
        if ensure_utc(expires_at) < utcnow():
            JusticeService._cache.pop(key, None)
            return None
        return copy.deepcopy(data)

    @staticmethod
    def _cache_set(key: str, value: dict[str, Any]) -> None:
        JusticeService._cache[key] = (utcnow() + _CACHE_TTL, copy.deepcopy(value))

    @staticmethod
    def _seed_from_match_id(match_id: Any) -> int:
        raw = str(match_id).encode("utf-8")
        digest = hashlib.sha256(raw).digest()
        return int.from_bytes(digest[:8], "big") % (2**32 - 1)

    @staticmethod
    def _is_valid_number(value: Any) -> bool:
        if not isinstance(value, (int, float)):
            return False
        return np.isfinite(float(value))

    @classmethod
    def calculate_match_xp(cls, match_v3: dict[str, Any]) -> dict[str, float]:
        """Run deterministic Poisson Monte Carlo and return xP probabilities."""
        teams = match_v3.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}

        xg_home = home.get("xg")
        xg_away = away.get("xg")
        if not cls._is_valid_number(xg_home) or not cls._is_valid_number(xg_away):
            raise ValueError("Match has invalid xG values.")

        lam_home = max(float(xg_home), 0.0)
        lam_away = max(float(xg_away), 0.0)

        seed = cls._seed_from_match_id(match_v3.get("_id"))
        rng = np.random.default_rng(seed=seed)
        home_goals = rng.poisson(lam=lam_home, size=_SIMULATIONS)
        away_goals = rng.poisson(lam=lam_away, size=_SIMULATIONS)

        home_wins = int(np.sum(home_goals > away_goals))
        away_wins = int(np.sum(away_goals > home_goals))
        draws = _SIMULATIONS - home_wins - away_wins

        home_win_prob = home_wins / _SIMULATIONS
        away_win_prob = away_wins / _SIMULATIONS
        draw_prob = draws / _SIMULATIONS

        expected_points_home = (3 * home_wins + draws) / _SIMULATIONS
        expected_points_away = (3 * away_wins + draws) / _SIMULATIONS

        return {
            "expected_points_home": expected_points_home,
            "expected_points_away": expected_points_away,
            "draw_prob": draw_prob,
            "home_win_prob": home_win_prob,
            "away_win_prob": away_win_prob,
        }

    async def get_unjust_table(self, league_id: int, season_id: int | None) -> dict[str, Any]:
        """Build unjust-table stats for a league/season from matches_v3."""
        resolved_season_id = season_id
        if resolved_season_id is None:
            reg = await _db.db.league_registry_v3.find_one({"_id": int(league_id)}, {"available_seasons": 1})
            seasons = (reg or {}).get("available_seasons") or []
            if not seasons:
                raise ValueError("League not found in registry.")
            current = max(seasons, key=lambda s: s.get("id") or 0)
            resolved_season_id = current.get("id")
            if not isinstance(resolved_season_id, int):
                raise ValueError("No valid season found.")

        cache_key = f"justice:{league_id}:{resolved_season_id}:{_CACHE_VERSION}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached_meta = dict(cached.get("calculation_meta") or {})
            cached_meta["cached"] = True
            cached["calculation_meta"] = cached_meta
            return cached

        rows = await _db.db.matches_v3.find(
            {
                "league_id": int(league_id),
                "season_id": int(resolved_season_id),
                "status": "FINISHED",
                "has_advanced_stats": True,
            },
            {
                "_id": 1,
                "start_at": 1,
                "teams": 1,
            },
        ).sort("start_at", 1).to_list(length=5000)

        if not rows:
            raise ValueError("No matches with xG data found for this league/season.")

        team_data: dict[int, dict[str, Any]] = defaultdict(
            lambda: {
                "name": "",
                "short_code": None,
                "image_path": None,
                "real_points": 0,
                "expected_points": 0.0,
                "total_xg": 0.0,
                "total_goals": 0,
                "total_conceded": 0,
                "xg_against": 0.0,
                "played": 0,
                "xp_values": [],
            }
        )

        included_matches = 0
        excluded_matches_count = 0

        for doc in rows:
            teams = doc.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}

            xg_h = home.get("xg")
            xg_a = away.get("xg")
            score_h = home.get("score")
            score_a = away.get("score")
            h_id = home.get("sm_id")
            a_id = away.get("sm_id")

            if not isinstance(h_id, int) or not isinstance(a_id, int):
                excluded_matches_count += 1
                continue
            if not self._is_valid_number(xg_h) or not self._is_valid_number(xg_a):
                excluded_matches_count += 1
                continue
            if not isinstance(score_h, int) or not isinstance(score_a, int):
                excluded_matches_count += 1
                continue

            try:
                xp = self.calculate_match_xp(doc)
            except ValueError:
                excluded_matches_count += 1
                continue

            included_matches += 1

            if score_h > score_a:
                rp_h, rp_a = 3, 0
            elif score_a > score_h:
                rp_h, rp_a = 0, 3
            else:
                rp_h, rp_a = 1, 1

            td_h = team_data[h_id]
            td_h["name"] = home.get("name") or td_h["name"]
            td_h["short_code"] = home.get("short_code") or td_h["short_code"]
            td_h["image_path"] = home.get("image_path") or td_h["image_path"]
            td_h["real_points"] += rp_h
            td_h["expected_points"] += float(xp["expected_points_home"])
            td_h["xp_values"].append(float(xp["expected_points_home"]))
            td_h["total_xg"] += float(xg_h)
            td_h["xg_against"] += float(xg_a)
            td_h["total_goals"] += int(score_h)
            td_h["total_conceded"] += int(score_a)
            td_h["played"] += 1

            td_a = team_data[a_id]
            td_a["name"] = away.get("name") or td_a["name"]
            td_a["short_code"] = away.get("short_code") or td_a["short_code"]
            td_a["image_path"] = away.get("image_path") or td_a["image_path"]
            td_a["real_points"] += rp_a
            td_a["expected_points"] += float(xp["expected_points_away"])
            td_a["xp_values"].append(float(xp["expected_points_away"]))
            td_a["total_xg"] += float(xg_a)
            td_a["xg_against"] += float(xg_h)
            td_a["total_goals"] += int(score_a)
            td_a["total_conceded"] += int(score_h)
            td_a["played"] += 1

        table: list[dict[str, Any]] = []
        for sm_id, td in team_data.items():
            played = int(td["played"])
            if not td["name"] or played <= 0:
                continue

            real_points = int(td["real_points"])
            expected_points_raw = float(td["expected_points"])
            total_xg = float(td["total_xg"])
            total_goals = int(td["total_goals"])
            total_conceded = int(td["total_conceded"])
            xg_against = float(td["xg_against"])

            expected_points = round(expected_points_raw, 2)
            luck_factor = round(real_points - expected_points_raw, 2)
            clinicality = round(total_goals - total_xg, 2)
            xg_diff = round(total_xg - xg_against, 2)
            real_gd = total_goals - total_conceded
            gd_justice = round(xg_diff - real_gd, 2)
            xp_arr = np.array(td["xp_values"], dtype=float)
            std_total = float(np.std(xp_arr)) * (played ** 0.5)
            ci_low = round(expected_points_raw - (1.96 * std_total), 2)
            ci_high = round(expected_points_raw + (1.96 * std_total), 2)
            last_5_xp = [round(v, 2) for v in reversed(td["xp_values"][-5:])]

            table.append(
                {
                    "team_sm_id": int(sm_id),
                    "team_name": td["name"],
                    "team_short_code": td["short_code"],
                    "team_image_path": td["image_path"],
                    "played": played,
                    "real_pts": real_points,
                    "expected_pts": expected_points,
                    "diff": round(expected_points_raw - real_points, 2),
                    "luck_factor": luck_factor,
                    "clinicality": clinicality,
                    "total_xg": round(total_xg, 2),
                    "total_goals": total_goals,
                    "total_conceded": total_conceded,
                    "avg_xg_for": round(total_xg / played, 2),
                    "avg_xg_against": round(xg_against / played, 2),
                    "xg_diff": xg_diff,
                    "real_gd": real_gd,
                    "gd_justice": gd_justice,
                    "luck_range": [ci_low, ci_high],
                    "last_5_xp": last_5_xp,
                }
            )

        table.sort(key=lambda row: (row["expected_pts"], row["real_pts"]), reverse=True)
        for index, row in enumerate(table, start=1):
            row["rank"] = index

        reg = await _db.db.league_registry_v3.find_one({"_id": int(league_id)}, {"name": 1})
        league_name = (reg or {}).get("name", "")

        result: dict[str, Any] = {
            "league_id": int(league_id),
            "league_name": league_name,
            "season_id": int(resolved_season_id),
            "match_count": included_matches,
            "excluded_matches_count": excluded_matches_count,
            "table": table,
            "calculation_meta": {
                "simulations": _SIMULATIONS,
                "cached": False,
                "generated_at_utc": utcnow(),
            },
        }
        self._cache_set(cache_key, result)
        return result
