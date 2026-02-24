"""Self-calibration worker — evaluates + optimizes Dixon-Coles parameters.

Daily (3 AM):   evaluate Brier scores, recalibrate degraded leagues.
Weekly (Mon 4 AM): refinement grid search around current params.
Monthly (1st 5 AM): full exploration grid search.
"""

import logging

from app.services.optimizer_service import run_calibration
from app.workers._state import set_synced

logger = logging.getLogger("quotico.calibration_worker")

_STATE_KEY = "calibration"


async def run_daily_evaluation() -> None:
    """Evaluate performance, recalibrate flagged leagues."""
    result = await run_calibration(force_mode=None)
    await set_synced(_STATE_KEY, metrics=result)
    logger.info("Daily calibration eval: %s", result)


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
