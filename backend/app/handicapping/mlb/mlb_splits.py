"""
MLB betting splits — line movement analysis + implied public betting.

Queries mlb.betting_lines_consolidated for opening vs closing line movement.
Same estimation heuristics as the NFL version but adapted for MLB
run line (±1.5 being the key number) and lower totals.
"""
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb.consolidated import MLBBettingLineConsolidated
from app.models.mlb import MLBGames, MLBSeason

logger = logging.getLogger("earl.mlb_splits")

# ── Split estimation ────────────────────────────

SPLIT_PER_POINT = 0.12


def _estimate_split(movement_pts: float) -> float:
    """Estimate the percentage of money on the side the line moved toward."""
    if movement_pts == 0:
        return 50.0
    sign = 1 if movement_pts > 0 else -1
    abs_move = abs(movement_pts)
    split = 50 + sign * min((abs_move * 12), 92 - 50)
    return round(split, 1)


def _moneyline_to_implied_prob(american_odds: int) -> float:
    if american_odds > 0:
        return round(100 / (american_odds + 100) * 100, 1)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100) * 100, 1)


class MLBBettingSplit:
    def __init__(self, game_id: int):
        self.game_id = game_id
        self.opening_spread: Optional[float] = None
        self.opening_over_under: Optional[float] = None
        self.opening_home_ml: Optional[int] = None
        self.opening_away_ml: Optional[int] = None
        self.current_spread: Optional[float] = None
        self.current_over_under: Optional[float] = None
        self.current_home_ml: Optional[int] = None
        self.current_away_ml: Optional[int] = None
        self.spread_movement: Optional[float] = None
        self.ou_movement: Optional[float] = None
        self.home_side_pct: Optional[float] = None
        self.over_pct: Optional[float] = None
        self.home_ml_implied_prob: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "opening_spread": self.opening_spread,
            "current_spread": self.current_spread,
            "spread_movement": self.spread_movement,
            "home_side_pct": self.home_side_pct,
            "opening_over_under": self.opening_over_under,
            "current_over_under": self.current_over_under,
            "ou_movement": self.ou_movement,
            "over_pct": self.over_pct,
            "opening_home_ml": self.opening_home_ml,
            "opening_away_ml": self.opening_away_ml,
            "current_home_ml": self.current_home_ml,
            "current_away_ml": self.current_away_ml,
            "home_ml_implied_prob": self.home_ml_implied_prob,
        }


class MLBSplitAnalyzer:
    """Builds betting split analysis for MLB games using consolidated opening/closing lines."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[MLBBettingSplit]:
        """Analyze splits for a single MLB game using consolidated opening vs closing lines."""
        r = await self.db.execute(
            select(MLBBettingLineConsolidated).where(
                MLBBettingLineConsolidated.game_id == game_id,
            ).limit(1)
        )
        cons = r.scalar_one_or_none()

        if not cons:
            logger.debug("No consolidated betting line found for game_id=%s", game_id)
            return None

        split = MLBBettingSplit(game_id)

        # Spread
        split.current_spread = cons.closing_spread
        split.opening_spread = cons.opening_spread
        if split.opening_spread is not None and split.current_spread is not None:
            split.spread_movement = round(
                split.current_spread - split.opening_spread, 1
            )
            split.home_side_pct = _estimate_split(split.spread_movement)

        # Over/Under
        split.current_over_under = cons.closing_ou
        split.opening_over_under = cons.opening_ou
        if split.opening_over_under is not None and split.current_over_under is not None:
            split.ou_movement = round(
                split.current_over_under - split.opening_over_under, 1
            )
            split.over_pct = _estimate_split(split.ou_movement)

        # Moneyline
        split.current_home_ml = cons.closing_home_ml
        split.current_away_ml = cons.closing_away_ml
        split.opening_home_ml = cons.opening_home_ml
        split.opening_away_ml = cons.opening_away_ml

        # Implied probability from closing moneyline
        home_ml = split.current_home_ml
        if home_ml is not None and home_ml != 0 and home_ml is not None:
            split.home_ml_implied_prob = _moneyline_to_implied_prob(home_ml)

        return split
