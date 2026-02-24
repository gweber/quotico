"""Backtest simulation utilities for Qbot strategy admin endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.utils import ensure_utc, parse_utc, utcnow

DEFAULT_BANKROLL = 1000.0
DEFAULT_BAYES_CONF = 0.333
DEFAULT_ODDS_FALLBACK = 3.0
DEFAULT_LOOKBACK_YEARS = 3
DEFAULT_LOOKBACK_MAX_TIPS = 1000
DEFAULT_ARENA_LOOKBACK_YEARS = 8
TIME_WEIGHT_FLOOR = 0.20
MAX_STAKE_BANKROLL_FRACTION = 0.05


def _signal_boosts(tip: dict) -> tuple[float, float, float]:
    """Extract sharp/momentum/rest boosts from a resolved tip."""
    signals = tip.get("tier_signals", {}) or {}
    pick = tip.get("recommended_selection")

    sharp_boost = 0.0
    sharp_sig = signals.get("sharp_movement", {}) or {}
    if sharp_sig.get("has_sharp_movement") and sharp_sig.get("direction") == pick:
        sharp_boost = 0.10
        if sharp_sig.get("is_late_money"):
            sharp_boost = 0.12
        if sharp_sig.get("has_steam_move") and sharp_sig.get("steam_outcome") == pick:
            sharp_boost += 0.03

    momentum_boost = 0.0
    momentum_sig = signals.get("momentum", {}) or {}
    gap = float(momentum_sig.get("gap", 0.0))
    if gap > 0.20:
        home_m = float((momentum_sig.get("home") or {}).get("momentum_score", 0.5))
        away_m = float((momentum_sig.get("away") or {}).get("momentum_score", 0.5))
        if (pick == "1" and home_m > away_m) or (pick == "2" and away_m > home_m):
            momentum_boost = 0.08

    rest_boost = 0.0
    rest_sig = signals.get("rest_advantage", {}) or {}
    if rest_sig.get("contributes"):
        diff = float(rest_sig.get("diff", 0.0))
        if (pick == "1" and diff > 0) or (pick == "2" and diff < 0):
            rest_boost = 0.04

    return sharp_boost, momentum_boost, rest_boost


def _resolve_tip_odds(
    tip: dict,
    odds_by_match: dict[str, dict[str, float]],
) -> float:
    """Resolve odds for a tip, preferring match odds over implied fallback."""
    match_id = str(tip.get("match_id", ""))
    selection = tip.get("recommended_selection")
    implied_prob = float(tip.get("implied_probability", 0.0))

    # Best source: historical match h2h odds for the selected outcome.
    if match_id and selection in {"1", "X", "2"}:
        h2h = odds_by_match.get(match_id) or {}
        sel_odds = h2h.get(selection)
        if isinstance(sel_odds, (int, float)) and float(sel_odds) > 1.0:
            return float(sel_odds)

    # Fallback: implied probability from tip.
    if implied_prob > 0.01:
        return float(1.0 / implied_prob)

    # Last resort fallback; avoid exaggerated 10.0 default.
    return DEFAULT_ODDS_FALLBACK


def _time_weight(
    match_date,
    *,
    reference_now,
    lookback_years: int,
) -> float:
    """Linear decay from 1.0 (today) to floor at lookback horizon."""
    if match_date is None:
        return TIME_WEIGHT_FLOOR
    dt = ensure_utc(match_date)
    days_old = max(0.0, (reference_now - dt).total_seconds() / 86400.0)
    horizon_days = max(1.0, float(lookback_years) * 365.0)
    return float(max(TIME_WEIGHT_FLOOR, 1.0 - (days_old / horizon_days)))


def _tip_profit_and_stake(
    tip: dict,
    dna: dict[str, float],
    *,
    bankroll: float,
    odds: float,
) -> tuple[float, float, bool, float]:
    """Compute (stake, profit, is_win, odds) for one tip under one DNA profile."""
    edge = float(tip.get("edge_pct", 0.0))
    conf = float(tip.get("confidence", 0.0))
    implied_prob = float(tip.get("implied_probability", 0.33))
    pick = tip.get("recommended_selection")
    is_win = bool(tip.get("was_correct", False))
    if implied_prob <= 0.01 and odds > 1.0:
        implied_prob = 1.0 / odds

    min_edge = float(dna.get("min_edge", 0.0))
    min_conf = float(dna.get("min_confidence", 0.0))
    sharp_weight = float(dna.get("sharp_weight", 1.0))
    momentum_weight = float(dna.get("momentum_weight", 1.0))
    rest_weight = float(dna.get("rest_weight", 1.0))
    kelly_fraction = float(dna.get("kelly_fraction", 0.0))
    max_stake = float(dna.get("max_stake", 0.0))
    home_bias = float(dna.get("home_bias", 1.0))
    away_bias = float(dna.get("away_bias", 1.0))
    h2h_weight = float(dna.get("h2h_weight", 0.0))
    draw_threshold = float(dna.get("draw_threshold", 0.0))
    volatility_buffer = float(dna.get("volatility_buffer", 0.0))
    bayes_trust_factor = float(dna.get("bayes_trust_factor", 0.0))

    sharp_boost, momentum_boost, rest_boost = _signal_boosts(tip)
    h2h_tip = float(((tip.get("tier_signals") or {}).get("poisson") or {}).get("h2h_weight", 0.0))
    bayes_conf = float(((tip.get("qbot_logic") or {}).get("bayesian_confidence", DEFAULT_BAYES_CONF))
    )

    adj_conf = conf
    adj_conf += sharp_weight * sharp_boost
    adj_conf += momentum_weight * momentum_boost
    adj_conf += rest_weight * rest_boost

    if pick == "1":
        adj_conf *= home_bias
    elif pick == "2":
        adj_conf *= away_bias

    adj_conf += h2h_weight * h2h_tip * 0.10
    blend_weight = min(max(bayes_trust_factor * 0.5, 0.0), 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_conf
    adj_conf = min(max(adj_conf, 0.0), 0.99)

    if edge < min_edge or conf < min_conf:
        return 0.0, 0.0, is_win, odds
    if pick == "X" and adj_conf < draw_threshold:
        return 0.0, 0.0, is_win, odds

    buffered_edge = max(adj_conf - implied_prob - volatility_buffer, 0.0)
    denom = max(odds - 1.0, 0.01)
    kelly_raw = kelly_fraction * buffered_edge / denom
    stake = float(bankroll) * max(kelly_raw, 0.0)
    risk_cap = float(bankroll) * MAX_STAKE_BANKROLL_FRACTION
    stake = min(stake, max_stake, risk_cap, float(bankroll))
    if stake <= 0.0:
        return 0.0, 0.0, is_win, odds

    profit = stake * (odds - 1.0) if is_win else -stake
    return float(stake), float(profit), is_win, float(odds)


async def simulate_strategy_backtest(
    strategy: dict[str, Any],
    *,
    starting_bankroll: float = DEFAULT_BANKROLL,
    limit_ledger: int | None = None,
    since_date: str | None = None,
) -> dict[str, Any]:
    """Simulate full-history bankroll curve for a strategy document."""
    sport_key = strategy.get("sport_key", "all")
    dna = strategy.get("dna", {}) or {}
    if not dna:
        return {
            "strategy_id": str(strategy.get("_id")),
            "sport_key": sport_key,
            "starting_bankroll": starting_bankroll,
            "ending_bankroll": starting_bankroll,
            "total_bets": 0,
            "wins": 0,
            "win_rate": 0.0,
            "points": [],
            "window": {},
        }

    query: dict[str, Any] = {"status": "resolved", "was_correct": {"$ne": None}}
    if sport_key and sport_key != "all":
        query["sport_key"] = sport_key

    window_meta: dict[str, Any] = {"mode": "fallback"}

    # 1) Explicit since_date wins.
    if since_date:
        since_dt = parse_utc(since_date)
        query["match_date"] = {"$gte": since_dt}
        window_meta = {
            "mode": "since_date",
            "since_date": since_dt.isoformat(),
            "until_date": None,
        }
    else:
        # 2) Validation-window sync (preferred): exact arena 80/20 validation slice.
        # First try persisted metadata, then reconstruct from historical tips prior
        # to strategy creation timestamp.
        validation_window = (
            ((strategy.get("optimization_notes") or {}).get("validation_window"))
            or {}
        )
        val_start = validation_window.get("start_date")
        val_end = validation_window.get("end_date")
        if val_start:
            start_dt = parse_utc(val_start)
            end_dt = parse_utc(val_end) if val_end else None
            query["match_date"] = {"$gte": start_dt}
            if end_dt is not None:
                query["match_date"]["$lte"] = end_dt
            window_meta = {
                "mode": "validation_window",
                "source": "strategy_metadata",
                "since_date": start_dt.isoformat(),
                "until_date": end_dt.isoformat() if end_dt else None,
            }
        else:
            created_at = ensure_utc(strategy.get("created_at", utcnow()))
            pre_query = {"status": "resolved", "was_correct": {"$ne": None}}
            if sport_key and sport_key != "all":
                pre_query["sport_key"] = sport_key
            pre_query["$or"] = [
                {"generated_at": {"$lte": created_at}},
                {"generated_at": {"$exists": False}, "match_date": {"$lte": created_at}},
            ]
            pre_tips = await _db.db.quotico_tips.find(
                pre_query,
                {"match_date": 1},
            ).sort("match_date", 1).to_list(length=200_000)

            if pre_tips:
                split_idx = int(len(pre_tips) * 0.80)
                val_tips = pre_tips[split_idx:] if split_idx < len(pre_tips) else []
                if val_tips:
                    start_dt = ensure_utc(val_tips[0]["match_date"])
                    end_dt = ensure_utc(val_tips[-1]["match_date"])
                    query["match_date"] = {"$gte": start_dt, "$lte": end_dt}
                    window_meta = {
                        "mode": "validation_window",
                        "source": "reconstructed_80_20",
                        "since_date": start_dt.isoformat(),
                        "until_date": end_dt.isoformat(),
                        "reconstructed_total_tips": len(pre_tips),
                        "reconstructed_validation_tips": len(val_tips),
                    }

    projection = {
        "_id": 0,
        "match_id": 1,
        "match_date": 1,
        "home_team": 1,
        "away_team": 1,
        "recommended_selection": 1,
        "edge_pct": 1,
        "confidence": 1,
        "implied_probability": 1,
        "was_correct": 1,
        "tier_signals": 1,
        "qbot_logic": 1,
    }
    tips = await _db.db.quotico_tips.find(query, projection).sort("match_date", 1).to_list(length=200_000)

    # 3) If no explicit/validation window is active, apply default lookback:
    # last 3 years and max last 1000 tips.
    if window_meta.get("mode") == "fallback" and tips:
        now = utcnow()
        cutoff = now - timedelta(days=365 * DEFAULT_LOOKBACK_YEARS)
        tips = [t for t in tips if ensure_utc(t["match_date"]) >= cutoff]
        if len(tips) > DEFAULT_LOOKBACK_MAX_TIPS:
            tips = tips[-DEFAULT_LOOKBACK_MAX_TIPS:]
        window_meta = {
            "mode": "default_lookback",
            "since_date": ensure_utc(tips[0]["match_date"]).isoformat() if tips else cutoff.isoformat(),
            "until_date": ensure_utc(tips[-1]["match_date"]).isoformat() if tips else now.isoformat(),
            "lookback_years": DEFAULT_LOOKBACK_YEARS,
            "lookback_max_tips": DEFAULT_LOOKBACK_MAX_TIPS,
        }

    # Load historical match odds once, then resolve odds per tip from match_id.
    odds_by_match: dict[str, dict[str, float]] = {}
    match_ids = sorted({str(t.get("match_id")) for t in tips if t.get("match_id")})
    object_ids = []
    for mid in match_ids:
        if len(mid) == 24:
            try:
                object_ids.append(ObjectId(mid))
            except Exception:
                continue
    if object_ids:
        matches = await _db.db.matches.find(
            {"_id": {"$in": object_ids}},
            {"_id": 1, "odds.h2h": 1},
        ).to_list(length=len(object_ids))
        for m in matches:
            h2h = ((m.get("odds") or {}).get("h2h") or {})
            odds_by_match[str(m["_id"])] = {
                "1": float(h2h.get("1", 0.0)) if h2h.get("1") is not None else 0.0,
                "X": float(h2h.get("X", 0.0)) if h2h.get("X") is not None else 0.0,
                "2": float(h2h.get("2", 0.0)) if h2h.get("2") is not None else 0.0,
            }

    bankroll = float(starting_bankroll)
    wins = 0
    total_bets = 0
    weighted_staked = 0.0
    weighted_profit = 0.0
    points: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    reference_now = utcnow()
    strategy_lookback_years = int(
        max(
            1,
            (
                ((strategy.get("optimization_notes") or {}).get("lookback_years"))
                or DEFAULT_ARENA_LOOKBACK_YEARS
            ),
        )
    )

    for tip in tips:
        odds = _resolve_tip_odds(tip, odds_by_match)
        stake, profit, is_win, odds = _tip_profit_and_stake(
            tip,
            dna,
            bankroll=bankroll,
            odds=odds,
        )
        if stake <= 0.0:
            continue
        bankroll_before = bankroll
        total_bets += 1
        if is_win:
            wins += 1
        bankroll += profit
        raw_match_date = tip.get("match_date")
        if raw_match_date is None:
            continue
        match_date = ensure_utc(raw_match_date)
        time_weight = _time_weight(
            match_date,
            reference_now=reference_now,
            lookback_years=strategy_lookback_years,
        )
        weighted_staked += stake * time_weight
        weighted_profit += profit * time_weight
        points.append(
            {
                "date": match_date.isoformat(),
                "bankroll": round(bankroll, 4),
                "is_win": bool(is_win),
                "stake": round(stake, 4),
                "match_id": tip.get("match_id"),
            }
        )
        ledger.append(
            {
                "date": match_date.isoformat(),
                "match": f"{tip.get('home_team', '-')} vs. {tip.get('away_team', '-')}",
                "home_team": tip.get("home_team"),
                "away_team": tip.get("away_team"),
                "edge_pct": round(float(tip.get("edge_pct", 0.0)), 4),
                "odds": round(float(odds), 4),
                "stake": round(float(stake), 4),
                "result": "win" if is_win else "loss",
                "net_profit": round(float(profit), 4),
                "bankroll_before": round(float(bankroll_before), 4),
                "bankroll_after": round(float(bankroll), 4),
                "selection": tip.get("recommended_selection"),
                "match_id": tip.get("match_id"),
                "time_weight": round(float(time_weight), 4),
                "weighted_net_profit": round(float(profit * time_weight), 4),
            }
        )

    win_rate = (wins / total_bets) if total_bets else 0.0
    weighted_roi = (weighted_profit / weighted_staked) if weighted_staked > 0 else 0.0
    ledger = list(reversed(ledger))
    if limit_ledger is not None and limit_ledger > 0:
        ledger = ledger[:limit_ledger]
    return {
        "strategy_id": str(strategy.get("_id")),
        "sport_key": sport_key,
        "starting_bankroll": float(starting_bankroll),
        "ending_bankroll": round(bankroll, 4),
        "total_bets": total_bets,
        "wins": wins,
        "win_rate": round(win_rate, 6),
        "weighted_roi": round(float(weighted_roi), 6),
        "weighted_profit": round(float(weighted_profit), 4),
        "weighted_staked": round(float(weighted_staked), 4),
        "points": points,
        "ledger": ledger,
        "window": window_meta,
    }
