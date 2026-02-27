"""
tools/qbot_ensemble_miner.py

Purpose:
    Robustness miner for Qbot DNA ensembles on v3.1/v3.2.
    Computes consensus with xROI stability, explicit xg_trust_factor CV and
    correlation-aware run diversity to avoid clone clusters.

Dependencies:
    - tools/qbot_evolution_arena.py
    - backend/app/database.py

Usage:
    python -m tools.qbot_ensemble_miner --sport soccer_epl
    python -m tools.qbot_ensemble_miner --multi --runs 7 --base-seed 42
    python -m tools.qbot_ensemble_miner --sport soccer_epl --mode deep
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Add backend to Python path
sys.path.insert(0, "backend")

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

CV_ROBUST_MAX = 0.10
CV_UNSTABLE_MIN = 0.30
CV_EPSILON = 1e-9
MAX_CLUSTER_CAP = 0.30
MAX_ACCEPTED_CORR = 0.92
_ARENA: Any = None


def _arena():
    global _ARENA
    if _ARENA is None:
        from tools import qbot_evolution_arena as arena_module
        _ARENA = arena_module
    return _ARENA


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _round_float(v: float, digits: int = 4) -> float:
    return round(float(v), digits)


def _cv(mean: float, std: float) -> float:
    denom = abs(float(mean))
    if denom < CV_EPSILON:
        return 0.0 if abs(float(std)) < CV_EPSILON else float("inf")
    return float(std) / denom


def _confidence_label(cv: float) -> str:
    if cv < CV_ROBUST_MAX:
        return "High"
    if cv > CV_UNSTABLE_MIN:
        return "Low"
    return "Medium"


def _stability_tag(cv: float) -> str:
    if cv < CV_ROBUST_MAX:
        return "robust"
    if cv > CV_UNSTABLE_MIN:
        return "unstable"
    return "medium"


def _print_gene_table(gene_rows: list[dict[str, Any]]) -> None:
    print("\nConsensus DNA Statistics")
    print("=" * 88)


def _cluster_label(dna: dict[str, float]) -> str:
    xg = float(dna.get("xg_trust_factor", 0.0))
    momentum = float(dna.get("momentum_weight", 0.0))
    sharp = float(dna.get("sharp_weight", 0.0))
    ref = float(dna.get("ref_cards_sensitivity", 0.0))
    if ref >= max(xg, momentum, sharp):
        return "referee_specialist"
    if xg >= max(momentum, sharp):
        return "justice_oracle"
    if sharp >= momentum:
        return "sharp_edge"
    return "momentum"


def _run_xroi(run: dict[str, Any]) -> float:
    val = run.get("validation_fitness") or {}
    if "xroi" in val:
        return float(val.get("xroi") or 0.0)
    return float(val.get("roi") or 0.0)


def _select_diverse_runs(valid: list[dict[str, Any]], genes: list[str]) -> list[int]:
    import numpy as np

    if len(valid) <= 2:
        return list(range(len(valid)))
    matrix = np.array(
        [[float((r.get("dna") or {}).get(g, 0.0)) for g in genes] for r in valid],
        dtype=np.float64,
    )
    corr = np.corrcoef(matrix)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    order = sorted(
        range(len(valid)),
        key=lambda i: (
            _run_xroi(valid[i]),
            float((valid[i].get("validation_fitness") or {}).get("roi", 0.0)),
        ),
        reverse=True,
    )
    cap = max(1, int(math.floor(len(valid) * MAX_CLUSTER_CAP)))
    selected: list[int] = []
    cluster_counts: dict[str, int] = {}
    for idx in order:
        dna = valid[idx].get("dna") or {}
        cluster = _cluster_label(dna)
        if cluster_counts.get(cluster, 0) >= cap:
            continue
        too_close = any(abs(float(corr[idx, s])) >= MAX_ACCEPTED_CORR for s in selected)
        if too_close:
            continue
        selected.append(idx)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
    if not selected:
        selected = order[: max(1, min(3, len(order)))]
    return selected
    header = f"{'Gene':<22}{'Mean':>12}{'StdDev':>12}{'CV':>10}{'Confidence':>13}{'Source':>19}"
    print(header)
    print("-" * 88)
    for row in gene_rows:
        cv_val = "inf" if math.isinf(row["cv"]) else f"{row['cv']:.4f}"
        print(
            f"{row['gene']:<22}"
            f"{row['mean']:>12.4f}"
            f"{row['std']:>12.4f}"
            f"{cv_val:>10}"
            f"{row['confidence']:>13}"
            f"{row['source']:>19}"
        )
    print("=" * 88)


async def _run_one_seed(
    sport_key: str | None,
    *,
    seed: int,
    mode: str,
    population_size: int,
    generations: int,
    candidate_workers: int,
    lookback_years: int,
) -> dict[str, Any]:
    if mode == "deep":
        result = await _arena().run_evolution_deep(
            sport_key,
            population_size=population_size,
            generations=generations,
            dry_run=True,
            seed=seed,
            resume=False,
            candidate_workers=candidate_workers,
            lookback_years=lookback_years,
        )
    else:
        result = await _arena().run_evolution(
            sport_key,
            population_size=population_size,
            generations=generations,
            dry_run=True,
            seed=seed,
            resume=False,
            search_mode=mode,
            candidate_workers=candidate_workers,
            lookback_years=lookback_years,
        )
    return result


def _build_consensus(
    run_results: list[dict[str, Any]],
) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, Any]]:
    import numpy as np

    valid = [r for r in run_results if "error" not in r and isinstance(r.get("dna"), dict)]
    if not valid:
        raise RuntimeError("No successful runs produced DNA.")

    genes = list(_arena().DNA_GENES)
    selected_indices = _select_diverse_runs(valid, genes)
    selected_runs = [valid[i] for i in selected_indices]

    best_run = max(
        selected_runs,
        key=lambda r: (
            _run_xroi(r),
            float((r.get("validation_fitness") or {}).get("roi", -1e9)),
            bool((r.get("stress_test") or {}).get("stress_passed", False)),
        ),
    )

    matrix = np.array(
        [[float((r.get("dna") or {}).get(g, 0.0)) for g in genes] for r in selected_runs],
        dtype=np.float64,
    )
    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0)

    consensus: dict[str, float] = {}
    gene_rows: list[dict[str, Any]] = []
    gene_stats: dict[str, Any] = {}
    source_by_gene: dict[str, str] = {}

    for idx, gene in enumerate(genes):
        mean = float(means[idx])
        std = float(stds[idx])
        cv = _cv(mean, std)
        tag = _stability_tag(cv)
        confidence = _confidence_label(cv)

        if tag == "unstable":
            value = float((best_run.get("dna") or {}).get(gene, mean))
            source = "best_seed"
        else:
            value = mean
            source = "ensemble_mean"

        consensus[gene] = _round_float(value, 6)
        source_by_gene[gene] = source

        gene_rows.append(
            {
                "gene": gene,
                "mean": mean,
                "std": std,
                "cv": cv,
                "confidence": confidence,
                "source": source,
            }
        )
        gene_stats[gene] = {
            "mean": _round_float(mean, 6),
            "std": _round_float(std, 6),
            "cv": (None if math.isinf(cv) else _round_float(cv, 6)),
            "stability": tag,
            "confidence": confidence,
            "source": source,
        }

    xg_gene = gene_stats.get("xg_trust_factor", {})
    xroi_values = np.array([_run_xroi(r) for r in selected_runs], dtype=np.float64)
    xroi_mean = float(xroi_values.mean()) if xroi_values.size else 0.0
    xroi_std = float(xroi_values.std()) if xroi_values.size else 0.0
    xroi_cv = _cv(xroi_mean, xroi_std)

    robust = [g for g in genes if gene_stats[g]["stability"] == "robust"]
    unstable = [g for g in genes if gene_stats[g]["stability"] == "unstable"]
    medium = [g for g in genes if gene_stats[g]["stability"] == "medium"]

    summary = {
        "successful_runs": len(valid),
        "selected_runs_for_consensus": len(selected_runs),
        "robust_genes": robust,
        "unstable_genes": unstable,
        "medium_genes": medium,
        "best_run_roi": _round_float(float((best_run.get("validation_fitness") or {}).get("roi", 0.0)), 6),
        "best_run_xroi": _round_float(_run_xroi(best_run), 6),
        "best_run_seed": best_run.get("optimization_notes", {}).get("seed"),
        "source_by_gene": source_by_gene,
        "gene_stats": gene_stats,
        "xroi_stability": {
            "mean": _round_float(xroi_mean, 6),
            "std": _round_float(xroi_std, 6),
            "cv": (None if math.isinf(xroi_cv) else _round_float(xroi_cv, 6)),
            "confidence": _confidence_label(xroi_cv),
        },
        "xg_trust_factor_cv": xg_gene.get("cv"),
        "diversity_controls": {
            "max_cluster_cap": MAX_CLUSTER_CAP,
            "max_pairwise_corr": MAX_ACCEPTED_CORR,
            "selected_indices": selected_indices,
        },
        "best_run": best_run,
    }
    return consensus, gene_rows, summary


async def _persist_ensemble_strategy(
    *,
    sport_key: str | None,
    mode: str,
    consensus_dna: dict[str, float],
    summary: dict[str, Any],
    run_results: list[dict[str, Any]],
    base_seed: int,
    runs: int,
    population_size: int,
    generations: int,
    dry_run: bool,
    with_archetypes: bool,
) -> dict[str, str | None]:
    import app.database as _db

    best_run = summary["best_run"]
    valid_runs = [r for r in run_results if "error" not in r and isinstance(r.get("dna"), dict)]
    seeds = [base_seed + i for i in range(runs)]
    sport_value = sport_key or "all"
    now = _utcnow()

    validation = best_run.get("validation_fitness") or {}
    training = best_run.get("training_fitness") or {}
    stress = best_run.get("stress_test") or {}

    strategy_doc = {
        "version": "v3",
        "fitness_version": "soft_penalty_v1",
        "stress_version": "stress_rescue_v1",
        "sport_key": sport_value,
        "generation": 0,
        "created_at": now,
        "dna": consensus_dna,
        "training_fitness": training,
        "validation_fitness": validation,
        "stress_test": stress,
        "is_active": False,
        "is_shadow": True,
        "is_ensemble": True,
        "archetype": "consensus",
        "population_size": population_size,
        "generations_run": generations,
        "evolution_time_s": sum(float(r.get("evolution_time_s", 0.0)) for r in run_results if "error" not in r),
        "fitness_history": {"best": [], "avg": []},
        "deployment_method": "ensemble_miner",
        "optimization_notes": {
            "schema_version": "v3.1",
            "ensemble": {
                "identity": "consensus",
                "mode": mode,
                "n_runs": runs,
                "base_seed": base_seed,
                "seeds": seeds,
                "successful_runs": summary["successful_runs"],
                "robust_threshold_cv_lt": CV_ROBUST_MAX,
                "unstable_threshold_cv_gt": CV_UNSTABLE_MIN,
                "gene_stats": summary["gene_stats"],
                "robust_genes": summary["robust_genes"],
                "unstable_genes": summary["unstable_genes"],
                "medium_genes": summary["medium_genes"],
                "source_by_gene": summary["source_by_gene"],
                "source_strategy_ids": [
                    r.get("_id")
                    for r in run_results
                    if isinstance(r.get("_id"), str)
                ],
                "xroi_stability": summary.get("xroi_stability"),
                "xg_trust_factor_cv": summary.get("xg_trust_factor_cv"),
                "diversity_controls": summary.get("diversity_controls"),
            },
            "why_shadow": "Ensemble consensus strategy requires explicit promotion.",
        },
    }

    inserted: dict[str, str | None] = {
        "consensus": None,
        "profit_hunter": None,
        "volume_grinder": None,
    }

    if dry_run:
        print(f"[DRY RUN] Would persist ensemble strategy for {sport_value}")
    else:
        result = await _db.db.qbot_strategies.insert_one(strategy_doc)
        inserted["consensus"] = str(result.inserted_id)
        print(f"Saved ensemble strategy (consensus): {inserted['consensus']}")

    if not with_archetypes or not valid_runs:
        return inserted

    profit_hunter = max(
        valid_runs,
        key=lambda r: float((r.get("validation_fitness") or {}).get("roi", -1e9)),
    )
    volume_grinder = max(
        valid_runs,
        key=lambda r: (
            int((r.get("validation_fitness") or {}).get("total_bets", 0)),
            float((r.get("validation_fitness") or {}).get("roi", -1e9)),
        ),
    )

    archetype_sources = {
        "profit_hunter": profit_hunter,
        "volume_grinder": volume_grinder,
    }
    for archetype, source in archetype_sources.items():
        doc = {
            "version": source.get("version", "v3"),
            "fitness_version": source.get("fitness_version", "soft_penalty_v1"),
            "stress_version": source.get("stress_version", "stress_rescue_v1"),
            "sport_key": source.get("sport_key", sport_value),
            "generation": source.get("generation", 0),
            "created_at": now,
            "dna": source.get("dna", {}),
            "training_fitness": source.get("training_fitness", {}),
            "validation_fitness": source.get("validation_fitness", {}),
            "stress_test": source.get("stress_test", {}),
            "is_active": False,
            "is_shadow": True,
            "is_ensemble": True,
            "archetype": archetype,
            "population_size": population_size,
            "generations_run": generations,
            "evolution_time_s": float(source.get("evolution_time_s", 0.0)),
            "fitness_history": source.get("fitness_history", {"best": [], "avg": []}),
            "deployment_method": "ensemble_miner",
            "optimization_notes": {
                "schema_version": "v3.1",
                "ensemble": {
                    "identity": archetype,
                    "mode": mode,
                    "n_runs": runs,
                    "base_seed": base_seed,
                    "seeds": seeds,
                    "picked_by": "highest_validation_roi" if archetype == "profit_hunter" else "highest_validation_bets",
                    "source_seed": (source.get("optimization_notes", {}) or {}).get("seed"),
                    "source_strategy_id": source.get("_id"),
                },
                "why_shadow": "Archetype strategy requires explicit promotion.",
            },
        }
        if dry_run:
            print(f"[DRY RUN] Would persist archetype strategy ({archetype}) for {sport_value}")
            continue
        r = await _db.db.qbot_strategies.insert_one(doc)
        inserted[archetype] = str(r.inserted_id)
        print(f"Saved ensemble strategy ({archetype}): {inserted[archetype]}")

    return inserted


async def _mine_sport(
    sport_key: str | None,
    *,
    runs: int,
    base_seed: int,
    mode: str,
    population_size: int,
    generations: int,
    candidate_workers: int,
    dry_run: bool,
    with_archetypes: bool,
    lookback_years: int,
) -> dict[str, Any]:
    sport_label = sport_key or "all"
    print(f"\n=== Ensemble mining: {sport_label} | runs={runs} | mode={mode} ===")

    run_results: list[dict[str, Any]] = []
    for i in range(runs):
        seed = base_seed + i
        print(f"Run {i + 1}/{runs} with seed={seed}")
        result = await _run_one_seed(
            sport_key,
            seed=seed,
            mode=mode,
            population_size=population_size,
            generations=generations,
            candidate_workers=candidate_workers,
            lookback_years=lookback_years,
        )
        run_results.append(result)
        if "error" in result:
            print(f"  -> failed: {result.get('error')}")
        else:
            roi = float((result.get("validation_fitness") or {}).get("roi", 0.0))
            print(f"  -> ROI={roi:+.4f}")

    consensus_dna, gene_rows, summary = _build_consensus(run_results)
    _print_gene_table(gene_rows)

    print("Robust genes :", ", ".join(summary["robust_genes"]) or "-")
    print("Unstable genes:", ", ".join(summary["unstable_genes"]) or "-")
    print("Medium genes  :", ", ".join(summary["medium_genes"]) or "-")
    xs = summary.get("xroi_stability") or {}
    print(
        "xROI stability:",
        f"mean={xs.get('mean', 0.0):+.4f}",
        f"std={xs.get('std', 0.0):.4f}",
        f"cv={xs.get('cv')}",
        f"confidence={xs.get('confidence', 'n/a')}",
    )
    print("xg_trust_factor CV:", summary.get("xg_trust_factor_cv"))

    inserted = await _persist_ensemble_strategy(
        sport_key=sport_key,
        mode=mode,
        consensus_dna=consensus_dna,
        summary=summary,
        run_results=run_results,
        base_seed=base_seed,
        runs=runs,
        population_size=population_size,
        generations=generations,
        dry_run=dry_run,
        with_archetypes=with_archetypes,
    )
    return {"sport_key": sport_label, "inserted": inserted, "summary": summary}


async def _discover_eligible_leagues() -> list[str]:
    discovered = await _arena().discover_leagues()
    return [
        sport_key
        for sport_key, count in discovered
        if count >= _arena().MIN_TIPS_PER_LEAGUE
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qbot Ensemble Miner — robust DNA mining across deterministic seeds",
    )
    parser.add_argument("--sport", type=str, default=None, help="sport_key (e.g. soccer_epl)")
    parser.add_argument("--multi", action="store_true", help="Run all eligible leagues")
    parser.add_argument("--runs", type=int, default=5, help="Number of runs per league (default: 5)")
    parser.add_argument("--base-seed", type=int, default=42, help="Seed start (default: 42)")
    parser.add_argument("--mode", choices=["quick", "deep"], default="quick", help="Evolution mode")
    parser.add_argument("--population", type=int, default=None, help="Population size override")
    parser.add_argument("--generations", type=int, default=None, help="Generations override")
    parser.add_argument("--candidate-workers", type=int, default=1, help="Per-run candidate stress workers")
    parser.add_argument("--lookback-years", type=int, default=8, help="Load only tips from the last N years")
    parser.add_argument("--with-archetypes", action="store_true", help="Also persist profit_hunter and volume_grinder")
    parser.add_argument("--dry-run", action="store_true", help="Do not persist ensemble strategy")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    import app.database as _db

    if args.runs < 2:
        raise ValueError("--runs must be >= 2 for variance analysis")
    if not args.multi and not args.sport:
        raise ValueError("Provide --sport or use --multi")

    defaults = _arena().MODE_DEFAULTS[args.mode]
    population_size = args.population or defaults["population_size"]
    generations = args.generations or defaults["generations"]

    await _db.connect_db()
    try:
        sports: list[str | None]
        if args.multi:
            sports = await _discover_eligible_leagues()
            if not sports:
                raise RuntimeError("No eligible leagues discovered for --multi")
        else:
            sports = [args.sport]

        for sport_key in sports:
            await _mine_sport(
                sport_key,
                runs=args.runs,
                base_seed=args.base_seed,
                mode=args.mode,
                population_size=population_size,
                generations=generations,
                candidate_workers=max(1, int(args.candidate_workers)),
                dry_run=args.dry_run,
                with_archetypes=bool(args.with_archetypes),
                lookback_years=max(1, int(args.lookback_years)),
            )
    finally:
        await _db.close_db()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
