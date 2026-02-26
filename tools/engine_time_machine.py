"""
Engine Time Machine — retroactive calibration across historical data.

Steps through history in configurable intervals, performing point-in-time
Dixon-Coles calibrations and (optionally) reliability analysis at each step.
Results are stored in the ``engine_config_history`` collection.

Each snapshot only sees data that existed at the snapshot date — no temporal
leakage.  The live ``engine_config`` document is never modified.

Usage:
    # Single league, monthly snapshots:
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga

    # All leagues, quarterly, with reliability:
    python -m tools.engine_time_machine --interval-days 90 --with-reliability

    # Resume an interrupted run (auto-detects last snapshot):
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga

    # Dry run (no DB writes):
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga --dry-run
"""

import argparse
import asyncio
import concurrent.futures
import logging
import math
import os
import sys
import time
from collections import defaultdict

# Add backend to Python path so we can import app modules
sys.path.insert(0, "backend")

# Default to local MongoDB when not set
if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engine_time_machine")


from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "engine_time_machine_v3"
DEFAULT_MODE = "auto"
MAX_ACCEPTABLE_RBS_WORSENING_PCT = 25.0
OUTLIER_WARN_GAP = 0.02
OUTLIER_CRITICAL_GAP = 0.05
XG_POISSON_MAX_GOALS = 10


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(dt: datetime, months: int = 1) -> datetime:
    total = (dt.year * 12 + (dt.month - 1)) + months
    year = total // 12
    month = (total % 12) + 1
    return dt.replace(year=year, month=month, day=1)


def _is_quarter_boundary(dt: datetime) -> bool:
    return dt.month in (1, 4, 7, 10)


def _get_rbs(snapshot: dict | None) -> float | None:
    if not snapshot:
        return None
    val = (snapshot.get("scores") or {}).get("regularized_brier")
    return float(val) if val is not None else None


def _snapshot_match_query(sport_key: str, step_date: datetime, window_days: int) -> dict:
    """Strict temporal-safe match query for snapshot analytics."""
    return {
        "sport_key": sport_key,
        "status": "final",
        "match_date": {
            "$gte": step_date - timedelta(days=window_days),
            "$lt": step_date,
        },
        "result.home_score": {"$exists": True},
        "result.away_score": {"$exists": True},
        "odds_meta.markets.h2h.current.1": {"$gt": 0},
        "odds_meta.markets.h2h.current.X": {"$gt": 0},
        "odds_meta.markets.h2h.current.2": {"$gt": 0},
    }


def _safe_float(value: object, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _outcome_from_scores(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "1"
    if away_goals > home_goals:
        return "2"
    return "X"


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def _probs_from_xg(home_xg: float, away_xg: float) -> dict[str, float]:
    """Independent Poisson 1/X/2 probabilities from xG values."""
    matrix = [[0.0] * (XG_POISSON_MAX_GOALS + 1) for _ in range(XG_POISSON_MAX_GOALS + 1)]
    total = 0.0
    for i in range(XG_POISSON_MAX_GOALS + 1):
        p_i = _poisson_pmf(i, max(home_xg, 0.0))
        for j in range(XG_POISSON_MAX_GOALS + 1):
            p_j = _poisson_pmf(j, max(away_xg, 0.0))
            v = p_i * p_j
            matrix[i][j] = v
            total += v

    if total <= 0:
        return {"1": 0.3333, "X": 0.3334, "2": 0.3333}

    home = 0.0
    draw = 0.0
    away = 0.0
    for i in range(XG_POISSON_MAX_GOALS + 1):
        for j in range(XG_POISSON_MAX_GOALS + 1):
            p = matrix[i][j] / total
            if i > j:
                home += p
            elif i < j:
                away += p
            else:
                draw += p
    return {"1": home, "X": draw, "2": away}


def _multiclass_brier(probs: dict[str, float], actual: str) -> float:
    total = 0.0
    for outcome in ("1", "X", "2"):
        o = 1.0 if outcome == actual else 0.0
        total += (float(probs.get(outcome, 0.0)) - o) ** 2
    return total


def _extract_h2h_market(match: dict) -> dict:
    return (((match.get("odds_meta") or {}).get("markets") or {}).get("h2h") or {})


def _extract_market_prices(match: dict, pick: str) -> tuple[float | None, float | None]:
    h2h = _extract_h2h_market(match)
    opening = _safe_float((h2h.get("opening") or {}).get(pick))
    closing = _safe_float((h2h.get("current") or {}).get(pick))
    return opening, closing


def _extract_market_uncertainty(match: dict) -> float | None:
    h2h = _extract_h2h_market(match)
    max_map = h2h.get("max") or {}
    min_map = h2h.get("min") or {}
    spreads: list[float] = []
    for k in ("1", "X", "2"):
        hi = _safe_float(max_map.get(k))
        lo = _safe_float(min_map.get(k))
        if hi is not None and lo is not None and hi >= lo:
            spreads.append(hi - lo)
    if spreads:
        return sum(spreads) / len(spreads)
    # fallback: closing spread around outcomes (coarser but available more often)
    current = h2h.get("current") or {}
    vals = [_safe_float(current.get(k)) for k in ("1", "X", "2")]
    vals = [v for v in vals if v is not None]
    if len(vals) >= 2:
        return max(vals) - min(vals)
    return None


def _build_snapshot_doc(
    sport_key: str,
    step_date: datetime,
    *,
    source: str,
    status: str,
    mode: str,
    evaluated: int,
    window_days: int,
    params: dict | None = None,
    scores: dict | None = None,
    baselines: dict | None = None,
    reliability: dict | None = None,
    extra_meta: dict | None = None,
) -> dict:
    meta = {
        "source": source,
        "status": status,
        "mode": mode,
        "is_retroactive": True,
        "matches_analyzed": int(evaluated),
        "window_start": (step_date - timedelta(days=window_days)).isoformat(),
        "window_end": step_date.isoformat(),
        "script_version": SCRIPT_VERSION,
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "sport_key": sport_key,
        "snapshot_date": step_date,
        "params": params,
        "scores": scores,
        "baselines": baselines,
        "reliability": reliability,
        "meta": meta,
    }


async def _load_snapshot_matches(
    db,
    *,
    sport_key: str,
    step_date: datetime,
    window_days: int,
) -> list[dict]:
    query = _snapshot_match_query(sport_key, step_date, window_days)
    projection = {
        "_id": 1,
        "sport_key": 1,
        "match_date": 1,
        "home_team_id": 1,
        "away_team_id": 1,
        "home_team": 1,
        "away_team": 1,
        "result.home_score": 1,
        "result.away_score": 1,
        "result.home_xg": 1,
        "result.away_xg": 1,
        "odds_meta.markets.h2h.current": 1,
        "odds_meta.markets.h2h.opening": 1,
        "odds_meta.markets.h2h.max": 1,
        "odds_meta.markets.h2h.min": 1,
    }
    return await db.matches.find(query, projection).sort("match_date", 1).to_list(length=5000)


async def _build_engine_predictions(
    matches: list[dict],
    *,
    step_date: datetime,
) -> dict[str, dict]:
    """
    Temporal-safe prediction map per match via existing tip generation pipeline.
    Key: str(match_id) -> {pick, probs, true_probability, edge_pct}
    """
    if not matches:
        return {}

    from app.services.quotico_tip_service import generate_quotico_tip

    sem = asyncio.Semaphore(8)
    results: dict[str, dict] = {}

    async def _predict_one(match: dict) -> None:
        async with sem:
            try:
                tip = await generate_quotico_tip(match, before_date=step_date)
            except Exception:
                return
        if not isinstance(tip, dict):
            return
        if tip.get("status") != "active":
            return
        pick = tip.get("recommended_selection")
        if pick not in {"1", "X", "2"}:
            return
        true_prob = _safe_float(tip.get("true_probability"))
        implied_prob = _safe_float(tip.get("implied_probability"))
        if true_prob is None or true_prob <= 0:
            return
        probs = (((tip.get("tier_signals") or {}).get("poisson") or {}).get("true_probs") or {})
        p1 = _safe_float(probs.get("1"))
        px = _safe_float(probs.get("X"))
        p2 = _safe_float(probs.get("2"))
        if p1 is None or px is None or p2 is None:
            return
        total = p1 + px + p2
        if total <= 0:
            return
        results[str(match["_id"])] = {
            "pick": pick,
            "probs": {"1": p1 / total, "X": px / total, "2": p2 / total},
            "true_probability": true_prob,
            "implied_probability": implied_prob,
            "edge_pct": _safe_float(tip.get("edge_pct"), 0.0) or 0.0,
        }

    await asyncio.gather(*(_predict_one(m) for m in matches))
    return results


def _compute_market_performance(matches: list[dict], predictions: dict[str, dict]) -> dict:
    clv_values: list[float] = []
    beat_total = 0
    beat_hits = 0
    uncertainty_values: list[float] = []
    opening_missing = 0
    closing_missing = 0

    per_pick: dict[str, dict[str, int]] = {
        "1": {"total": 0, "beats": 0},
        "X": {"total": 0, "beats": 0},
        "2": {"total": 0, "beats": 0},
    }

    for m in matches:
        pred = predictions.get(str(m.get("_id")))
        if not pred:
            continue
        pick = pred["pick"]
        opening, closing = _extract_market_prices(m, pick)
        if opening is None:
            opening_missing += 1
        if closing is None:
            closing_missing += 1
        if opening is None or closing is None:
            continue

        per_pick[pick]["total"] += 1
        beat_total += 1
        clv_values.append(opening - closing)

        uncertainty = _extract_market_uncertainty(m)
        if uncertainty is not None:
            uncertainty_values.append(uncertainty)

        engine_prob = float(pred["true_probability"])
        fair_odds = (1.0 / engine_prob) if engine_prob > 0 else None
        if fair_odds is None:
            continue
        if abs(fair_odds - closing) < abs(fair_odds - opening):
            beat_hits += 1
            per_pick[pick]["beats"] += 1

    def _rate(a: int, b: int) -> float | None:
        return round(a / b, 4) if b > 0 else None

    return {
        "avg_clv": round(sum(clv_values) / len(clv_values), 6) if clv_values else None,
        "beat_closing_rate": _rate(beat_hits, beat_total),
        "market_uncertainty_index": (
            round(sum(uncertainty_values) / len(uncertainty_values), 6)
            if uncertainty_values else None
        ),
        "sample_size": beat_total,
        "opening_missing_rate": _rate(opening_missing, len(matches)),
        "closing_missing_rate": _rate(closing_missing, len(matches)),
        "beat_closing_rate_by_pick": {
            k: _rate(v["beats"], v["total"]) for k, v in per_pick.items()
        },
    }


def _tag_archetypes(match: dict, pred: dict) -> list[str]:
    tags: list[str] = []
    edge_pct = float(pred.get("edge_pct", 0.0))
    pick = pred.get("pick")
    opening, closing = _extract_market_prices(match, pick)
    uncertainty = _extract_market_uncertainty(match)
    match_dt = match.get("match_date")
    hour = match_dt.hour if isinstance(match_dt, datetime) else None

    if edge_pct >= 10.0:
        tags.append("value_oracle")
    if opening is not None and closing is not None and opening > 0:
        drift = (opening - closing) / opening
        if drift >= 0.03:
            tags.append("steam_snatcher")
        if drift > 0 and (uncertainty is None or uncertainty <= 0.25):
            tags.append("sharp_hunter")
    if hour is not None and hour >= 22:
        tags.append("night_owl")
    if not tags:
        tags.append("steady_hand")
    return tags


def _compute_archetype_backtest(matches: list[dict], predictions: dict[str, dict]) -> dict:
    perf: dict[str, dict[str, float | int]] = defaultdict(lambda: {"hits": 0, "total": 0, "brier_sum": 0.0})
    for m in matches:
        pred = predictions.get(str(m.get("_id")))
        if not pred:
            continue
        result = m.get("result") or {}
        hs = _safe_int(result.get("home_score"))
        aw = _safe_int(result.get("away_score"))
        if hs is None or aw is None:
            continue
        actual = _outcome_from_scores(hs, aw)
        probs = pred.get("probs") or {}
        brier = _multiclass_brier(probs, actual)
        hit = int((pred.get("pick") or "") == actual)
        for tag in _tag_archetypes(m, pred):
            perf[tag]["total"] += 1
            perf[tag]["hits"] += hit
            perf[tag]["brier_sum"] += brier

    archetypes: dict[str, dict] = {}
    best_key = None
    best_tuple = (-1.0, -1.0, -1)  # hit_rate desc, avg_brier inverse, total desc
    for name, p in perf.items():
        total = int(p["total"])
        if total <= 0:
            continue
        hit_rate = float(p["hits"]) / total
        avg_brier = float(p["brier_sum"]) / total
        archetypes[name] = {
            "total": total,
            "hits": int(p["hits"]),
            "hit_rate": round(hit_rate, 4),
            "avg_brier": round(avg_brier, 6),
        }
        score_tuple = (hit_rate, -avg_brier, total)
        if score_tuple > best_tuple:
            best_tuple = score_tuple
            best_key = name

    return {
        "best_archetype": best_key,
        "best_confidence": round(best_tuple[0], 4) if best_key else None,
        "archetypes": archetypes,
    }


def _compute_statistical_integrity(
    matches: list[dict],
    predictions: dict[str, dict],
    *,
    pure_brier: float | None,
) -> dict:
    xg_deltas: list[float] = []
    efficiencies: list[float] = []
    xg_brier_values: list[float] = []
    skipped_missing_xg = 0
    used_xg = 0

    for m in matches:
        result = m.get("result") or {}
        hs = _safe_int(result.get("home_score"))
        aw = _safe_int(result.get("away_score"))
        hxg = _safe_float(result.get("home_xg"))
        axg = _safe_float(result.get("away_xg"))
        if hs is None or aw is None:
            continue
        if hxg is None or axg is None:
            skipped_missing_xg += 1
            continue

        used_xg += 1
        xg_deltas.append((hs + aw) - (hxg + axg))
        efficiencies.append((hs + aw) / max(hxg + axg, 0.01))

        pred = predictions.get(str(m.get("_id")))
        if not pred:
            continue
        engine_probs = pred.get("probs") or {}
        if not engine_probs:
            continue
        xg_probs = _probs_from_xg(hxg, axg)
        bs = 0.0
        for outcome in ("1", "X", "2"):
            bs += (float(engine_probs.get(outcome, 0.0)) - float(xg_probs.get(outcome, 0.0))) ** 2
        xg_brier_values.append(bs)

    avg_xg_delta = round(sum(xg_deltas) / len(xg_deltas), 6) if xg_deltas else None
    clinical_eff = round(sum(efficiencies) / len(efficiencies), 6) if efficiencies else None
    xg_brier = round(sum(xg_brier_values) / len(xg_brier_values), 6) if xg_brier_values else None
    gap = None
    level = "none"
    outlier_detected = False
    if pure_brier is not None and xg_brier is not None:
        gap = round(float(pure_brier) - float(xg_brier), 6)
        if gap >= OUTLIER_CRITICAL_GAP:
            level = "critical"
            outlier_detected = True
        elif gap >= OUTLIER_WARN_GAP:
            level = "warn"
            outlier_detected = True

    return {
        "avg_xg_delta": avg_xg_delta,
        "clinical_efficiency_avg": clinical_eff,
        "xg_brier_score": xg_brier,
        "pure_vs_xg_brier_gap": gap,
        "outlier_level": level,
        "outlier_detected": outlier_detected,
        "used_xg_matches": used_xg,
        "skipped_missing_xg": skipped_missing_xg,
    }


def _compute_team_efficiency_dna(matches: list[dict]) -> dict:
    teams: dict[str, dict] = defaultdict(lambda: {
        "team_id": None,
        "team_name": None,
        "matches": 0,
        "goals": 0.0,
        "xg": 0.0,
    })
    skipped_missing_team_ids = 0

    for m in matches:
        result = m.get("result") or {}
        hs = _safe_int(result.get("home_score"))
        aw = _safe_int(result.get("away_score"))
        hxg = _safe_float(result.get("home_xg"))
        axg = _safe_float(result.get("away_xg"))
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        if hs is None or aw is None or hxg is None or axg is None:
            continue
        if not home_id or not away_id:
            skipped_missing_team_ids += 1
            continue
        home_key = str(home_id)
        away_key = str(away_id)
        teams[home_key]["team_id"] = home_key
        teams[home_key]["team_name"] = m.get("home_team")
        teams[home_key]["matches"] += 1
        teams[home_key]["goals"] += hs
        teams[home_key]["xg"] += hxg

        teams[away_key]["team_id"] = away_key
        teams[away_key]["team_name"] = m.get("away_team")
        teams[away_key]["matches"] += 1
        teams[away_key]["goals"] += aw
        teams[away_key]["xg"] += axg

    rows = []
    for row in teams.values():
        if row["matches"] <= 0:
            continue
        delta = row["goals"] - row["xg"]
        eff = row["goals"] / max(row["xg"], 0.01)
        rows.append({
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "matches": int(row["matches"]),
            "goals": round(float(row["goals"]), 3),
            "xg": round(float(row["xg"]), 3),
            "delta": round(float(delta), 3),
            "efficiency": round(float(eff), 4),
        })

    over = sorted(rows, key=lambda r: r["delta"], reverse=True)[:3]
    under = sorted(rows, key=lambda r: r["delta"])[:3]
    return {
        "overperformers_top3": over,
        "underperformers_top3": under,
        "skipped_missing_team_ids": skipped_missing_team_ids,
    }


def _build_xp_table(matches: list[dict]) -> dict:
    teams: dict[str, dict] = defaultdict(lambda: {
        "team_id": None,
        "team_name": None,
        "played": 0,
        "xp": 0.0,
        "xg_for": 0.0,
        "xg_against": 0.0,
    })
    skipped_missing_team_ids = 0
    for m in matches:
        result = m.get("result") or {}
        hxg = _safe_float(result.get("home_xg"))
        axg = _safe_float(result.get("away_xg"))
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        if hxg is None or axg is None:
            continue
        if not home_id or not away_id:
            skipped_missing_team_ids += 1
            continue
        probs = _probs_from_xg(hxg, axg)
        home_xp = 3.0 * probs["1"] + probs["X"]
        away_xp = 3.0 * probs["2"] + probs["X"]
        hk = str(home_id)
        ak = str(away_id)

        teams[hk]["team_id"] = hk
        teams[hk]["team_name"] = m.get("home_team")
        teams[hk]["played"] += 1
        teams[hk]["xp"] += home_xp
        teams[hk]["xg_for"] += hxg
        teams[hk]["xg_against"] += axg

        teams[ak]["team_id"] = ak
        teams[ak]["team_name"] = m.get("away_team")
        teams[ak]["played"] += 1
        teams[ak]["xp"] += away_xp
        teams[ak]["xg_for"] += axg
        teams[ak]["xg_against"] += hxg

    table = []
    for row in teams.values():
        table.append({
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "played": int(row["played"]),
            "xp": round(float(row["xp"]), 3),
            "xg_for": round(float(row["xg_for"]), 3),
            "xg_against": round(float(row["xg_against"]), 3),
            "xg_diff": round(float(row["xg_for"] - row["xg_against"]), 3),
        })
    table.sort(key=lambda r: (r["xp"], r["xg_diff"]), reverse=True)
    for idx, row in enumerate(table, 1):
        row["rank"] = idx
    return {"table": table, "skipped_missing_team_ids": skipped_missing_team_ids}


async def _export_justice_table_to_mongo(
    db,
    *,
    sport_key: str,
    step_date: datetime,
    window_days: int,
    matches: list[dict],
    dry_run: bool,
) -> bool:
    from app.utils import utcnow

    payload = _build_xp_table(matches)
    if dry_run:
        return False
    doc = {
        "sport_key": sport_key,
        "snapshot_date": step_date,
        "window_start": step_date - timedelta(days=window_days),
        "window_end": step_date,
        "table": payload["table"],
        "meta": {
            "source": "time_machine_v3",
            "script_version": SCRIPT_VERSION,
            "generated_at": utcnow(),
            "skipped_missing_team_ids": payload["skipped_missing_team_ids"],
        },
    }
    await db.engine_time_machine_justice.update_one(
        {"sport_key": sport_key, "snapshot_date": step_date},
        {"$set": doc},
        upsert=True,
    )
    return True


async def _find_earliest_match(db, sport_key: str) -> datetime | None:
    """Find the earliest resolved match with odds for a league."""
    earliest = await db.matches.find_one(
        {
            "sport_key": sport_key,
            "status": "final",
            "odds_meta.markets.h2h.current.1": {"$gt": 0},
            "odds_meta.markets.h2h.current.X": {"$gt": 0},
            "odds_meta.markets.h2h.current.2": {"$gt": 0},
            "result.home_score": {"$exists": True},
        },
        {"match_date": 1},
        sort=[("match_date", 1)],
    )
    if earliest:
        from app.utils import ensure_utc
        return ensure_utc(earliest["match_date"])
    return None


async def _find_latest_snapshot(db, sport_key: str) -> datetime | None:
    """Find the most recent snapshot date for resume logic."""
    latest = await db.engine_config_history.find_one(
        {
            "sport_key": sport_key,
            "$or": [
                {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
                {"meta.is_retroactive": True},
            ],
        },
        {"snapshot_date": 1},
        sort=[("snapshot_date", -1)],
    )
    if latest:
        from app.utils import ensure_utc
        return ensure_utc(latest["snapshot_date"])
    return None


async def _clear_retro_snapshots(
    db,
    sport_key: str,
    *,
    dry_run: bool,
) -> int:
    """Delete only retro time-machine snapshots for a league."""
    query = {
        "sport_key": sport_key,
        "$or": [
            {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
            {"meta.is_retroactive": True},
        ],
    }
    if dry_run:
        return int(await db.engine_config_history.count_documents(query))
    result = await db.engine_config_history.delete_many(query)
    return int(result.deleted_count)


async def _process_league_inner(
    sport_key: str,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    with_market_beat: bool,
    with_xg_justice: bool,
    export_justice_table: bool,
    max_snapshots: int = 0,
) -> dict:
    """Process a single league through historical time steps."""
    import app.database as _db
    from app.services.optimizer_service import (
        CALIBRATION_WINDOW_DAYS,
        MIN_CALIBRATION_MATCHES,
        calibrate_league,
    )
    from app.utils import ensure_utc, utcnow

    db = _db.db
    now = utcnow()

    # Determine start date
    earliest = await _find_earliest_match(db, sport_key)
    if not earliest:
        log.warning("  %s: no eligible matches found — skipping", sport_key)
        return {"sport_key": sport_key, "status": "no_data"}

    # Need at least 1 year of data for the first calibration window
    first_viable = earliest + timedelta(days=365)
    if first_viable >= now:
        log.warning("  %s: not enough history (earliest=%s) — skipping",
                    sport_key, earliest.strftime("%Y-%m-%d"))
        return {"sport_key": sport_key, "status": "insufficient_history",
                "earliest": earliest.isoformat()}

    # Resume from last snapshot if available
    latest_snapshot = await _find_latest_snapshot(db, sport_key)
    if latest_snapshot:
        if interval_days == 30:
            start = _month_start(_add_months(latest_snapshot, 1))
        else:
            start = latest_snapshot + timedelta(days=interval_days)
        log.info("  %s: resuming from %s (last snapshot: %s)",
                 sport_key, start.strftime("%Y-%m-%d"),
                 latest_snapshot.strftime("%Y-%m-%d"))
    else:
        if interval_days == 30:
            start = _month_start(first_viable)
            if start < first_viable:
                start = _add_months(start, 1)
        else:
            start = first_viable
        log.info("  %s: starting from %s (earliest match: %s)",
                 sport_key, start.strftime("%Y-%m-%d"),
                 earliest.strftime("%Y-%m-%d"))

    # Generate date steps
    steps: list[datetime] = []
    if interval_days == 30:
        current = start
        while current < now:
            steps.append(current)
            current = _add_months(current, 1)
    else:
        current = start
        while current < now:
            steps.append(current)
            current += timedelta(days=interval_days)

    if not steps:
        log.info("  %s: already up to date", sport_key)
        return {"sport_key": sport_key, "status": "up_to_date",
                "last_snapshot": latest_snapshot.isoformat() if latest_snapshot else None}
    if max_snapshots and max_snapshots > 0:
        steps = steps[:max_snapshots]

    log.info("  %s: %d snapshots to compute (%s → %s)",
             sport_key, len(steps),
             steps[0].strftime("%Y-%m-%d"),
             steps[-1].strftime("%Y-%m-%d"))

    snapshots_written = 0
    snapshots_skipped = 0
    snapshots_carried = 0
    errors = 0
    t0 = time.monotonic()

    existing_snapshots = await db.engine_config_history.find(
        {
            "sport_key": sport_key,
            "$or": [
                {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
                {"meta.is_retroactive": True},
            ],
        },
        {"snapshot_date": 1, "params": 1, "scores": 1, "baselines": 1, "reliability": 1},
    ).sort("snapshot_date", 1).to_list(length=5000)
    for snap in existing_snapshots:
        if snap.get("snapshot_date") is not None:
            snap["snapshot_date"] = ensure_utc(snap["snapshot_date"])

    prev_snapshot: dict | None = None
    existing_idx = 0
    if steps:
        first_step = steps[0]
        while existing_idx < len(existing_snapshots) and existing_snapshots[existing_idx]["snapshot_date"] < first_step:
            prev_snapshot = existing_snapshots[existing_idx]
            existing_idx += 1

    for i, step_date in enumerate(steps, 1):
        step_t0 = time.monotonic()
        try:
            prev_rbs = _get_rbs(prev_snapshot)
            effective_mode = mode
            if mode == "auto":
                effective_mode = "exploration" if _is_quarter_boundary(step_date) else "refinement"
                if prev_rbs is not None and prev_rbs > 0:
                    # Force exploration if previous step already degraded hard.
                    # This keeps runtime low in normal months while reacting to drift.
                    if prev_rbs > 0.40:
                        effective_mode = "exploration"

            try:
                result = await asyncio.wait_for(
                    calibrate_league(sport_key, mode=effective_mode, before_date=step_date),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                errors += 1
                log.error(
                    "  [%d/%d] %s @ %s: TIMEOUT (300s)",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"),
                )
                continue

            status = result.get("status", "error")
            if status != "calibrated":
                carried = False
                if prev_snapshot:
                    carry_doc = _build_snapshot_doc(
                        sport_key,
                        step_date,
                        source="time_machine_carry_forward",
                        status=f"carry_forward_{status}",
                        mode=effective_mode,
                        evaluated=int(result.get("evaluated") or 0),
                        window_days=CALIBRATION_WINDOW_DAYS,
                        params=prev_snapshot.get("params"),
                        scores=prev_snapshot.get("scores"),
                        baselines=prev_snapshot.get("baselines"),
                        reliability=prev_snapshot.get("reliability"),
                    )
                    if not dry_run:
                        await db.engine_config_history.update_one(
                            {"sport_key": sport_key, "snapshot_date": step_date},
                            {"$set": carry_doc},
                            upsert=True,
                        )
                        prev_snapshot = carry_doc
                    carried = True
                    snapshots_carried += 1
                else:
                    snapshots_skipped += 1
                log.info(
                    "  [%d/%d] %s @ %s: %s (N=%s)%s",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"),
                    status.upper(),
                    result.get("matches") or result.get("data_points") or "?",
                    " [carried]" if carried else "",
                )
                continue

            evaluated = int(result.get("evaluated") or 0)
            candidate_rbs = float(result["regularized_brier"])
            quality_reject = False
            quality_reason = None
            if evaluated < MIN_CALIBRATION_MATCHES:
                quality_reject = True
                quality_reason = f"insufficient_matches:{evaluated}"
            elif prev_rbs is not None and prev_rbs > 0:
                worsening_pct = ((candidate_rbs - prev_rbs) / prev_rbs) * 100.0
                if worsening_pct > MAX_ACCEPTABLE_RBS_WORSENING_PCT:
                    quality_reject = True
                    quality_reason = f"rbs_worsened:{worsening_pct:.2f}%"

            if quality_reject and prev_snapshot:
                carry_doc = _build_snapshot_doc(
                    sport_key,
                    step_date,
                    source="time_machine_carry_forward",
                    status="carry_forward_quality_gate",
                    mode=effective_mode,
                    evaluated=evaluated,
                    window_days=CALIBRATION_WINDOW_DAYS,
                    params=prev_snapshot.get("params"),
                    scores=prev_snapshot.get("scores"),
                    baselines=prev_snapshot.get("baselines"),
                    reliability=prev_snapshot.get("reliability"),
                    extra_meta={
                        "quality_reason": quality_reason,
                        "candidate_scores": {
                            "pure_brier": result.get("pure_brier"),
                            "regularized_brier": result.get("regularized_brier"),
                        },
                    },
                )
                if not dry_run:
                    await db.engine_config_history.update_one(
                        {"sport_key": sport_key, "snapshot_date": step_date},
                        {"$set": carry_doc},
                        upsert=True,
                    )
                    prev_snapshot = carry_doc
                snapshots_carried += 1
                log.info(
                    "  [%d/%d] %s @ %s: QUALITY-GATE (%s) [carried]",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"), quality_reason,
                )
                continue

            # Build snapshot document
            market_performance = None
            archetype_backtest = None
            statistical_integrity = None
            team_efficiency_dna = None
            prediction_map: dict[str, dict] = {}

            if with_market_beat or with_xg_justice or export_justice_table:
                window_matches = await _load_snapshot_matches(
                    db,
                    sport_key=sport_key,
                    step_date=step_date,
                    window_days=CALIBRATION_WINDOW_DAYS,
                )
                if with_market_beat or with_xg_justice:
                    prediction_map = await _build_engine_predictions(
                        window_matches,
                        step_date=step_date,
                    )
                if with_market_beat:
                    market_performance = _compute_market_performance(window_matches, prediction_map)
                    archetype_backtest = _compute_archetype_backtest(window_matches, prediction_map)
                if with_xg_justice:
                    statistical_integrity = _compute_statistical_integrity(
                        window_matches,
                        prediction_map,
                        pure_brier=_safe_float(result.get("pure_brier")),
                    )
                    team_efficiency_dna = _compute_team_efficiency_dna(window_matches)

            snapshot = _build_snapshot_doc(
                sport_key,
                step_date,
                source="time_machine",
                status="calibrated",
                mode=effective_mode,
                evaluated=evaluated,
                window_days=CALIBRATION_WINDOW_DAYS,
                params={
                    "rho": result["rho"],
                    "alpha": result["alpha"],
                    "floor": result["floor"],
                },
                scores={
                    "pure_brier": result["pure_brier"],
                    "regularized_brier": result["regularized_brier"],
                    "calibration_error": result["calibration_error"],
                },
                baselines=result.get("baselines"),
                reliability=None,
                extra_meta={
                    "landscape_range": result.get("landscape_range"),
                    "schema_version": "v3",
                    "with_market_beat": with_market_beat,
                    "with_xg_justice": with_xg_justice,
                    "justice_exported": False,
                },
            )
            if market_performance is not None:
                snapshot["market_performance"] = market_performance
            if statistical_integrity is not None:
                snapshot["statistical_integrity"] = statistical_integrity
            if archetype_backtest is not None:
                snapshot["archetype_backtest"] = archetype_backtest
            if team_efficiency_dna is not None:
                snapshot["team_efficiency_dna"] = team_efficiency_dna

            # Optional reliability analysis
            if with_reliability:
                try:
                    from app.services.reliability_service import analyze_engine_reliability
                    rel = await analyze_engine_reliability(
                        sport_key, before_date=step_date,
                    )
                    if rel:
                        snapshot["reliability"] = {
                            "multiplier": rel["multiplier"],
                            "cap": rel["cap"],
                            "regression_factor": rel["regression_factor"],
                            "avg_win_rate": rel["avg_win_rate"],
                        }
                except Exception:
                    log.debug("  Reliability analysis failed for %s @ %s",
                              sport_key, step_date.strftime("%Y-%m-%d"))

            if not dry_run:
                await db.engine_config_history.update_one(
                    {"sport_key": sport_key, "snapshot_date": step_date},
                    {"$set": snapshot},
                    upsert=True,
                )
                if export_justice_table:
                    exported = await _export_justice_table_to_mongo(
                        db,
                        sport_key=sport_key,
                        step_date=step_date,
                        window_days=CALIBRATION_WINDOW_DAYS,
                        matches=window_matches,
                        dry_run=dry_run,
                    )
                    if exported:
                        await db.engine_config_history.update_one(
                            {"sport_key": sport_key, "snapshot_date": step_date},
                            {"$set": {"meta.justice_exported": True}},
                        )
                        snapshot["meta"]["justice_exported"] = True
                prev_snapshot = snapshot

            step_elapsed = time.monotonic() - step_t0
            baselines_str = ""
            if result.get("baselines"):
                b = result["baselines"]
                baselines_str = f"  H={b['avg_home']:.2f} A={b['avg_away']:.2f}"

            rel_str = ""
            if snapshot.get("reliability"):
                r = snapshot["reliability"]
                rel_str = f"  rel={r['multiplier']:.2f}"

            log.info(
                "  [%d/%d] %s @ %s: ρ=%.2f α=%.3f floor=%.2f  "
                "BS=%.4f%s%s  (N=%d, %.1fs)%s [mode=%s]",
                i, len(steps), sport_key,
                step_date.strftime("%Y-%m-%d"),
                result["rho"], result["alpha"], result["floor"],
                result["pure_brier"],
                baselines_str, rel_str,
                result["evaluated"], step_elapsed,
                " [DRY]" if dry_run else "", effective_mode,
            )
            snapshots_written += 1

        except Exception as e:
            step_elapsed = time.monotonic() - step_t0
            errors += 1
            log.error("  [%d/%d] %s @ %s: ERROR: %s (%.1fs)",
                      i, len(steps), sport_key,
                      step_date.strftime("%Y-%m-%d"), e, step_elapsed)

    total_elapsed = time.monotonic() - t0
    log.info("  %s: done — %d written, %d carried, %d skipped, %d errors (%.1fs total)",
             sport_key, snapshots_written, snapshots_carried, snapshots_skipped, errors, total_elapsed)

    return {
        "sport_key": sport_key,
        "status": "completed",
        "snapshots_written": snapshots_written,
        "snapshots_carried": snapshots_carried,
        "snapshots_skipped": snapshots_skipped,
        "errors": errors,
        "elapsed": round(total_elapsed, 1),
    }


def _process_league_sync(
    sport_key: str,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    with_market_beat: bool,
    with_xg_justice: bool,
    export_justice_table: bool,
    max_snapshots: int,
) -> dict:
    """Sync wrapper for per-league time-machine run in a dedicated process."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        import app.database as _db
        loop.run_until_complete(_db.connect_db())
        try:
            result = loop.run_until_complete(
                _process_league_inner(
                    sport_key=sport_key,
                    interval_days=interval_days,
                    mode=mode,
                    dry_run=dry_run,
                    with_reliability=with_reliability,
                    with_market_beat=with_market_beat,
                    with_xg_justice=with_xg_justice,
                    export_justice_table=export_justice_table,
                    max_snapshots=max_snapshots,
                )
            )
            return result
        finally:
            loop.run_until_complete(_db.close_db())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def run_time_machine(
    sport_key: str | None,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    with_market_beat: bool,
    with_xg_justice: bool,
    export_justice_table: bool,
    concurrency: int,
    rerun: bool,
    max_snapshots: int,
) -> None:
    import app.database as _db
    from app.services.optimizer_service import CALIBRATED_LEAGUES

    await _db.connect_db()
    try:
        log.info("Connected to MongoDB: %s", _db.db.name)

        target_leagues = [sport_key] if sport_key else CALIBRATED_LEAGUES
        if len(target_leagues) != len(set(target_leagues)):
            log.warning("Duplicate leagues detected in target set — de-duplicating")
        target_leagues = list(dict.fromkeys(target_leagues))
        assert len(target_leagues) == len(set(target_leagues)), "Duplicate leagues detected"
        log.info("=== ENGINE TIME MACHINE ===")
        log.info("Leagues: %d | Interval: %dd | Mode: %s | Reliability: %s | MarketBeat: %s | xGJustice: %s | xPExport: %s | Rerun: %s%s",
                 len(target_leagues), interval_days, mode,
                 "ON" if with_reliability else "OFF",
                 "ON" if with_market_beat else "OFF",
                 "ON" if with_xg_justice else "OFF",
                 "ON" if export_justice_table else "OFF",
                 "ON" if rerun else "OFF",
                 " [DRY RUN]" if dry_run else "")

        if rerun:
            for league in target_leagues:
                deleted = await _clear_retro_snapshots(_db.db, league, dry_run=dry_run)
                if dry_run:
                    log.info("  %s: would delete %d retro snapshots", league, deleted)
                else:
                    log.info("  %s: deleted %d retro snapshots", league, deleted)

        t0 = time.monotonic()
        results: dict[str, dict] = {}
        if concurrency <= 1 or len(target_leagues) <= 1:
            for league in target_leagues:
                try:
                    results[league] = await _process_league_inner(
                        league,
                        interval_days,
                        mode,
                        dry_run,
                        with_reliability,
                        with_market_beat,
                        with_xg_justice,
                        export_justice_table,
                        max_snapshots,
                    )
                except Exception as e:
                    log.error("League %s failed: %s", league, e)
                    results[league] = {"error": str(e)}
        else:
            max_workers = min(len(target_leagues), max(1, int(concurrency)))
            with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        _process_league_sync,
                        league,
                        interval_days,
                        mode,
                        dry_run,
                        with_reliability,
                        with_market_beat,
                        with_xg_justice,
                        export_justice_table,
                        max_snapshots,
                    ): league
                    for league in target_leagues
                }
                for future in concurrent.futures.as_completed(futures):
                    league = futures[future]
                    try:
                        results[league] = future.result()
                    except Exception as e:
                        log.error("League %s failed: %s", league, e)
                        results[league] = {"error": str(e)}

        total_elapsed = time.monotonic() - t0

        # Summary
        log.info("=" * 60)
        log.info("TIME MACHINE COMPLETE%s (%.1fs)", " [DRY RUN]" if dry_run else "", total_elapsed)
        for league in target_leagues:
            result = results.get(league) or {}
            if "error" in result:
                log.error("  %-30s  EXCEPTION: %s", league, result["error"])
                continue
            status = result.get("status", "?")
            if status == "completed":
                log.info("  %-30s  %d snapshots, %d carried, %d skipped, %d errors (%.1fs)",
                         league, result["snapshots_written"],
                         result.get("snapshots_carried", 0),
                         result["snapshots_skipped"], result["errors"],
                         result["elapsed"])
            else:
                log.info("  %-30s  %s", league, str(status).upper())
        log.info("=" * 60)
    finally:
        await _db.close_db()


def main():
    parser = argparse.ArgumentParser(
        description="Engine Time Machine — retroactive calibration across historical data",
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Filter to one sport_key (default: all calibrated leagues)")
    parser.add_argument("--interval-days", type=int, default=30,
                        help="Step size in days (default: 30 = monthly)")
    parser.add_argument("--mode", type=str, default=DEFAULT_MODE,
                        choices=["auto", "exploration", "refinement"],
                        help="Grid search mode (default: auto)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--with-reliability", action="store_true",
                        help="Also compute reliability stats at each snapshot")
    parser.add_argument("--with-market-beat", action="store_true",
                        help="Compute CLV/closing-line market beat analytics per snapshot")
    parser.add_argument("--with-xg-justice", action="store_true",
                        help="Compute xG justice metrics and outlier detection per snapshot")
    parser.add_argument("--export-justice-table", action="store_true",
                        help="Export xP justice table to engine_time_machine_justice per snapshot")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Max leagues to process in parallel (default: 8)")
    parser.add_argument("--max-snapshots", type=int, default=0,
                        help="Limit number of snapshots per league (0 = all)")
    parser.add_argument("--rerun", action="store_true",
                        help="Delete retro time-machine snapshots first, then rebuild from scratch")

    args = parser.parse_args()

    asyncio.run(run_time_machine(
        sport_key=args.sport,
        interval_days=args.interval_days,
        mode=args.mode,
        dry_run=args.dry_run,
        with_reliability=args.with_reliability,
        with_market_beat=args.with_market_beat,
        with_xg_justice=args.with_xg_justice,
        export_justice_table=args.export_justice_table,
        concurrency=args.concurrency,
        rerun=args.rerun,
        max_snapshots=args.max_snapshots,
    ))


if __name__ == "__main__":
    main()
