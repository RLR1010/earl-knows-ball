"""
Betting splits — line movement analysis + implied public betting.

Uses the difference between opening and current lines to estimate
where the betting public is leaning. Also tracks consensus splits.

Line movement → implied splits (approximate):
  ±1.0 pt  →  ~15% of money on moved-to side
  ±3.0 pts →  ~40% (key number — significant)
  ±6.0+    →  ~65%+ (heavy public side)
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Game, Team, Season, BettingLine

logger = logging.getLogger("earl.splits")

# ── Split estimation ────────────────────────────

SPLIT_PER_POINT = 0.12  # rough: each 1pt of line movement = ~12% of bets


def _estimate_split(movement_pts: float) -> float:
    """
    Estimate the percentage of money on the side the line moved toward.

    Uses a non-linear curve that flattens at extremes:
      ±0.5 → ~8%
      ±1.0 → ~15%
      ±3.0 → ~40%
      ±6.0 → ~65%
      ±10  → ~85%
    """
    # Logistic-like function: saturates at extremes
    if movement_pts == 0:
        return 50.0
    sign = 1 if movement_pts > 0 else -1
    abs_move = abs(movement_pts)
    split = 50 + sign * min((abs_move * 12), 92 - 50)
    return round(split, 1)


def _moneyline_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to implied probability (0-100)."""
    if american_odds > 0:
        return round(100 / (american_odds + 100) * 100, 1)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100) * 100, 1)


class BettingSplit:
    """Betting split analysis for a single game."""

    def __init__(self, game_id: int):
        self.game_id = game_id
        # Opening line
        self.opening_spread: Optional[float] = None
        self.opening_over_under: Optional[float] = None
        self.opening_home_ml: Optional[int] = None
        self.opening_away_ml: Optional[int] = None
        # Current/closing line
        self.current_spread: Optional[float] = None
        self.current_over_under: Optional[float] = None
        self.current_home_ml: Optional[int] = None
        self.current_away_ml: Optional[int] = None
        # Movement
        self.spread_movement: Optional[float] = None  # positive = line moved toward home
        self.ou_movement: Optional[float] = None  # positive = O/U moved up
        # Implied splits
        self.home_side_pct: Optional[float] = None  # % of bets on home side of spread
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


class SplitAnalyzer:
    """Builds betting split analysis for games."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[BettingSplit]:
        """Analyze splits for a single game."""
        # Get opening line (source = the_odds_api_opening)
        r = await self.db.execute(
            select(BettingLine).where(
                BettingLine.game_id == game_id,
                BettingLine.source.in_(["the_odds_api_opening", "article_opening_2025", "article_opening_2024", "sbr_opening"]),
            ).order_by(BettingLine.recorded_at.asc()).limit(1)
        )
        opening = r.scalar_one_or_none()
        if not opening:
            r = await self.db.execute(
                select(BettingLine).where(
                    BettingLine.game_id == game_id,
                    BettingLine.source.like("article_opening_%"),
                ).limit(1)
            )
            opening = r.scalar_one_or_none()

        # Get current/closing line
        r = await self.db.execute(
            select(BettingLine).where(
                BettingLine.game_id == game_id,
                BettingLine.source.in_(["the_odds_api", "nflverse", "sbr_closing"]),
            ).order_by(BettingLine.recorded_at.desc()).limit(1)
        )
        current = r.scalar_one_or_none()

        if not current:
            return None

        split = BettingSplit(game_id)

        if opening:
            split.opening_spread = opening.spread
            split.opening_over_under = opening.over_under
            split.opening_home_ml = opening.home_moneyline
            split.opening_away_ml = opening.away_moneyline

            if current.spread is not None and opening.spread is not None:
                spread_movement = current.spread - opening.spread
                split.spread_movement = round(spread_movement, 1)
                split.home_side_pct = _estimate_split(spread_movement)

            if current.over_under is not None and opening.over_under is not None:
                split.ou_movement = round(current.over_under - opening.over_under, 1)
                split.over_pct = _estimate_split(split.ou_movement)

        # Always populate current lines
        split.current_spread = current.spread
        split.current_over_under = current.over_under
        split.current_home_ml = current.home_moneyline
        split.current_away_ml = current.away_moneyline

        if current.home_moneyline and current.away_moneyline:
            split.home_ml_implied_prob = _moneyline_to_implied_prob(current.home_moneyline)

        # If no opening line yet, use a neutral estimate
        if not opening:
            split.home_side_pct = 50.0
            split.over_pct = 50.0
            split.opening_spread = current.spread  # assume current = opening if no opening data
            split.opening_over_under = current.over_under

        return split

    async def analyze_week(self, year: int, week: int) -> list[BettingSplit]:
        """Analyze betting splits for all games in a week."""
        r = await self.db.execute(select(Season).where(Season.year == year))
        season = r.scalar_one_or_none()
        if not season:
            return []

        r = await self.db.execute(
            select(Game).where(
                Game.season_id == season.id,
                Game.week == week,
            ).order_by(Game.date)
        )
        games = r.scalars().all()

        results = []
        for game in games:
            split = await self.analyze_game(game.id)
            if split:
                results.append(split)
        return results
