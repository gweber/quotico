from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Abstract base class for odds data providers."""

    @abstractmethod
    async def get_odds(self, league_id: int) -> list[dict[str, Any]]:
        """Fetch current odds for a given league.

        Returns a list of match dicts with at least:
        - external_id: str
        - league_id: int
        - teams: {"home": str, "away": str}
        - commence_time: datetime
        - odds: {"1": float, "X": float, "2": float} or {"1": float, "2": float}
        """
        ...

    @abstractmethod
    async def get_scores(self, league_id: int) -> list[dict[str, Any]]:
        """Fetch completed match results for a given league.

        Returns a list of dicts with:
        - external_id: str
        - completed: bool
        - result: str (e.g. "1", "X", "2")
        - scores: list of score dicts
        """
        ...
