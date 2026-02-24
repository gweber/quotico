"""Self-calibration worker — evaluates + optimizes Dixon-Coles parameters.

Daily (3 AM):   evaluate Brier scores, recalibrate degraded leagues.
Weekly (Mon 4 AM): refinement grid search around current params.
Monthly (1st 5 AM): full exploration grid search.
Sunday (23:00): reliability analysis — meta-learning confidence calibration.
"""

import logging

from app.services.optimizer_service import run_calibration
from app.services.qbot_intelligence_service import update_cluster_stats
from app.services.reliability_service import run_reliability_analysis
from app.workers._state import set_synced

logger = logging.getLogger("quotico.calibration_worker")

_STATE_KEY = "calibration"
_RELIABILITY_STATE_KEY = "reliability"
_QBOT_STATE_KEY = "qbot_clusters"


async def run_daily_evaluation() -> None:
    """Evaluate performance, recalibrate flagged leagues, update Qbot clusters."""
    result = await run_calibration(force_mode=None)
    await set_synced(_STATE_KEY, metrics=result)
    logger.info("Daily calibration eval: %s", result)

    # Update Qbot Bayesian cluster stats (lightweight — pure aggregation)
    try:
        qbot_result = await update_cluster_stats()
        await set_synced(_QBOT_STATE_KEY, metrics=qbot_result)
        logger.info("Qbot cluster stats: %s", qbot_result)
    except Exception:
        logger.warning("Qbot cluster update failed", exc_info=True)


async def run_weekly_refinement() -> None:
    """Narrow grid search around current params."""
    result = await run_calibration(force_mode="refinement")
    await set_synced(_STATE_KEY, metrics=result)
    logger.info("Weekly refinement: %s", result)


async def run_monthly_exploration() -> None:
    """Full grid search across entire parameter space."""
    result = await run_calibration(force_mode="exploration")
    await set_synced(_STATE_KEY, metrics=result)
    logger.info("Monthly exploration: %s", result)


async def run_reliability_check() -> None:
    """Weekly reliability analysis — recalibrate confidence parameters.

    Analyzes historical tip performance per league and confidence band,
    then updates the engine_config.reliability subdocument with meta-learned
    multiplier, cap, and regression_factor.
    """
    result = await run_reliability_analysis()
    await set_synced(_RELIABILITY_STATE_KEY, metrics=result)
    logger.info("Reliability analysis: %s", result)
