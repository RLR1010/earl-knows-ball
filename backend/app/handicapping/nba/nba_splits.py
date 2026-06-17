"""
NBA betting splits — line movement analysis + implied public betting.

Uses nba.betting_lines table for opening vs current line comparison.
Adapted for NBA where spread movement is more significant than MLB run line.
"""
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBABettingLine, NBAGame, NBASeason

logger = logging.getLogger("earl.nba_splits")

# ── Split estimation ────────────────────────────

SPLIT_PER_POINT = 0.08


def _estimate_split(movement_pts: float) -> float:
    """Estimate the percentage of money on the side the line moved toward.

    In NBA, each point of line movement represents ~8% of money on that side.
    """
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


class NBABettingSplit:
    """Betting split analysis for a single NBA game."""

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


class NBASplitAnalyzer:
    """Builds betting split analysis for NBA games."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[NBABettingSplit]:
        """Analyze splits for a single NBA game using opening vs current lines."""
        # Get opening line
        r = await self.db.execute(
            select(NBABettingLine).where(
                NBABettingLine.game_id == game_id,
                NBABettingLine.is_opening == "true",
            ).limit(1)
        )
        opening = r.scalar_one_or_none()

        if not opening:
            # Fall back to earliest recorded line
            r = await self.db.execute(
                select(NBABettingLine).where(
                    NBABettingLine.game_id == game_id,
                ).order_by(NBABettingLine.recorded_at.asc()).limit(1)
            )
            opening = r.scalar_one_or_none()

        # Get latest recorded line
        r = await self.db.execute(
            select(NBABettingLine).where(
                NBABettingLine.game_id == game_id,
            ).order_by(NBABettingLine.recorded_at.desc()).limit(1)
        )
        current = r.scalar_one_or_none()

        if not current:
            return None

        split = NBABettingSplit(game_id)
        split.current_spread = current.spread
        split.current_over_under = current.over_under
        split.current_home_ml = current.home_moneyline
        split.current_away_ml = current.away_moneyline

        if opening:
            split.opening_spread = opening.opening_spread or opening.spread
            split.opening_over_under = opening.opening_total or opening.over_under
            split.opening_home_ml = opening.opening_home_moneyline or opening.home_moneyline
            split.opening_away_ml = opening.opening_away_moneyline or opening.away_moneyline

            if current.spread is not None and opening.spread is not None:
                spread_movement = current.spread - opening.spread
                split.spread_movement = round(spread_movement, 1)
                split.home_side_pct = _estimate_split(spread_movement)

            if current.over_under is not None and opening.over_under is not None:
                split.ou_movement = round(current.over_under - opening.over_under, 1)
                split.over_pct = _estimate_split(split.ou_movement)

        if current.home_moneyline and current.away_moneyline:
            split.home_ml_implied_prob = _moneyline_to_implied_prob(current.home_moneyline)

        if not opening:
            split.home_side_pct = 50.0
            split.over_pct = 50.0
            split.opening_spread = current.spread
            split.opening_over_under = current.over_under

        return split

    async def analyze_date(self, game_date: str) -> list[NBABettingSplit]:
        """Analyze betting splits for all games on a given date."""
        from datetime import datetime, timezone, timedelta
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        r = await self.db.execute(
            select(NBAGame.id).where(
                NBAGame.date >= dt,
                NBAGame.date < dt + timedelta(days=1),
            ).order_by(NBAGame.date)
        )
        game_ids = [row[0] for row in r.fetchall()]
        results = []
        for gid in game_ids:
            split = await self.analyze_game(gid)
            if split:
                results.append(split)
        return results

    async def analyze_games(self, game_ids: list[int]) -> list[NBABettingSplit]:
        """Analyze betting splits for a list of game IDs."""
        results = []
        for gid in game_ids:
            split = await self.analyze_game(gid)
            if split:
                results.append(split)
        return results
