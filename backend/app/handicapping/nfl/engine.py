"""
Earl Knows Ball — NFL game handicapping engine.

Synthesizes betting lines + team stats to produce structured pick cards
with confidence scores for every NFL game.

Supports week-by-week backtesting via max_week parameter.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Game, Team, Season, BettingLine, GamePrediction, GameLines
from app.handicapping.nfl.situational import SituationalAnalyzer
from app.handicapping.nfl.splits import SplitAnalyzer
from app.handicapping.nfl.nfl_xgb_model_ou import predict_total as xgb_predict_ou_total
from app.handicapping.nfl.nfl_xgb_model_ats import predict_margin as xgb_predict_margin_ats
from app.handicapping.nfl.nfl_xgb_model_ml import predict_home_win_prob as xgb_predict_ml_prob


logger = logging.getLogger("earl.handicapping")

# ── Constants ──────────────────────────────────────────────────────────

CURRENT_SEASON = 2026


# ── Team Strength Metrics ──────────────────────────────────────────────

class TeamStats:
    """Per-team, per-season aggregated offensive and defensive stats."""

    def __init__(self, team_abbr: str, team_id: int, year: int):
        self.team_abbr = team_abbr
        self.team_id = team_id
        self.year = year
        self.games = 0
        self.home_games = 0
        self.away_games = 0
        self.points_for = 0.0
        self.points_against = 0.0
        self.ats_wins = 0
        self.ats_losses = 0
        self.ats_pushes = 0
        self.over_hits = 0
        self.under_hits = 0
        self.over_under_pushes = 0
        self.ml_wins = 0
        self.ml_losses = 0
        self.underdog_wins = 0
        self.underdog_games = 0
        self.home_points_for = 0.0
        self.home_points_against = 0.0
        self.away_points_for = 0.0
        self.away_points_against = 0.0
        self.recent_form = []

    @property
    def ppg_for(self) -> float:
        return round(self.points_for / max(self.games, 1), 2)

    @property
    def ppg_against(self) -> float:
        return round(self.points_against / max(self.games, 1), 2)

    @property
    def scoring_margin(self) -> float:
        return round(self.ppg_for - self.ppg_against, 2)

    @property
    def home_ppg_for(self) -> float:
        return round(self.home_points_for / max(self.home_games, 1), 2)

    @property
    def home_ppg_against(self) -> float:
        return round(self.home_points_against / max(self.home_games, 1), 2)

    @property
    def away_ppg_for(self) -> float:
        return round(self.away_points_for / max(self.away_games, 1), 2)

    @property
    def away_ppg_against(self) -> float:
        return round(self.away_points_against / max(self.away_games, 1), 2)

    @property
    def ats_pct(self) -> Optional[float]:
        total = self.ats_wins + self.ats_losses
        return round(self.ats_wins / max(total, 1), 3) if total > 0 else None

    @property
    def over_pct(self) -> Optional[float]:
        total = self.over_hits + self.under_hits
        return round(self.over_hits / max(total, 1), 3) if total > 0 else None

    @property
    def ml_pct(self) -> Optional[float]:
        total = self.ml_wins + self.ml_losses
        return round(self.ml_wins / max(total, 1), 3) if total > 0 else None

    @property
    def underdog_ats_pct(self) -> Optional[float]:
        total = self.underdog_games
        return round(self.underdog_wins / max(total, 1), 3) if total > 0 else None

    @property
    def recent_form_str(self) -> str:
        return "-".join(self.recent_form[-5:]) if self.recent_form else "N/A"

    @property
    def win_pct(self) -> Optional[float]:
        total = self.ml_wins + self.ml_losses
        return round(self.ml_wins / max(total, 1), 3) if total > 0 else None

    def to_dict(self) -> dict:
        return {
            "team": self.team_abbr,
            "year": self.year,
            "games": self.games,
            "ppg_for": self.ppg_for,
            "ppg_against": self.ppg_against,
            "scoring_margin": self.scoring_margin,
            "home_ppg_for": self.home_ppg_for,
            "home_ppg_against": self.home_ppg_against,
            "away_ppg_for": self.away_ppg_for,
            "away_ppg_against": self.away_ppg_against,
            "ats_pct": self.ats_pct,
            "ml_pct": self.ml_pct,
            "over_pct": self.over_pct,
            "win_pct": self.win_pct,
            "underdog_ats_pct": self.underdog_ats_pct,
            "recent_form": self.recent_form_str,
        }


# ── Team Stats Builder ────────────────────────────────────────────────

class TeamStatsBuilder:
    """Builds team strength metrics from historical game data."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        year: int,
        num_games: Optional[int] = None,
        start_week: int = 1,
        max_week: Optional[int] = None,
    ) -> dict[str, TeamStats]:
        """
        Build TeamStats for all teams in a given season.

        For backtesting, use max_week=N to see what you'd know before week N.
        For display (all games up to current), omit max_week.

        Args:
            year: Season year
            num_games: Only use last N games per team
            start_week: First week to include
            max_week: If set, ONLY use games with week < max_week
        """
        season_r = await self.db.execute(select(Season).where(Season.year == year))
        season = season_r.scalar_one_or_none()
        if not season:
            return {}

        # For week 1 backtest (max_week <= 1), use previous season's full stats
        use_year = year
        if max_week is not None and max_week <= 1:
            prev_season_r = await self.db.execute(
                select(Season).where(Season.year == year - 1)
            )
            prev = prev_season_r.scalar_one_or_none()
            if prev:
                use_year = year - 1
                max_week = None  # use all of previous season
                start_week = 1

        # Build WHERE conditions
        r_season_r = await self.db.execute(select(Season).where(Season.year == use_year))
        r_season = r_season_r.scalar_one_or_none()
        if not r_season:
            return {}

        conditions = [
            Game.season_id == r_season.id,
            Game.game_type == "REG",
            Game.home_score.isnot(None),
            Game.away_score.isnot(None),
        ]
        if max_week is not None:
            conditions.append(Game.week < max_week)
            conditions.append(Game.week >= start_week)
        else:
            conditions.append(Game.week >= start_week)

        r = await self.db.execute(
            select(Game).where(*conditions).order_by(Game.week, Game.date)
        )
        all_season_games = r.scalars().all()

        if not all_season_games:
            return {}

        # Load betting lines for stats games
        line_r = await self.db.execute(
            select(BettingLine).where(
                BettingLine.game_id.in_([g.id for g in all_season_games])
            )
        )
        lines = {bl.game_id: bl for bl in line_r.scalars().all()}

        team_r = await self.db.execute(select(Team))
        teams = {t.id: t.abbreviation for t in team_r.scalars().all()}

        # Init stats
        all_stats: dict[str, TeamStats] = {}
        for tid, tabbr in teams.items():
            all_stats[tabbr] = TeamStats(tabbr, tid, year)

        # Apply num_games filter: keep the most recent N games per team
        if num_games:
            sorted_by_date = sorted(
                all_season_games,
                key=lambda g: (g.date or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )
            team_count: dict[str, int] = defaultdict(int)
            games_to_use = []
            for g in sorted_by_date:
                ha = teams.get(g.home_team_id, "")
                aa = teams.get(g.away_team_id, "")
                if team_count[ha] >= num_games and team_count[aa] >= num_games:
                    continue
                team_count[ha] += 1
                team_count[aa] += 1
                games_to_use.append(g)
        else:
            games_to_use = all_season_games

        # Process games into stats
        for g in games_to_use:
            home_abbr = teams.get(g.home_team_id, "")
            away_abbr = teams.get(g.away_team_id, "")
            if not home_abbr or not away_abbr:
                continue

            home = all_stats[home_abbr]
            away = all_stats[away_abbr]
            home_score = g.home_score or 0
            away_score = g.away_score or 0
            line = lines.get(g.id)

            # Scoring totals
            home.games += 1
            home.home_games += 1
            home.points_for += home_score
            home.points_against += away_score
            home.home_points_for += home_score
            home.home_points_against += away_score

            away.games += 1
            away.away_games += 1
            away.points_for += away_score
            away.points_against += home_score
            away.away_points_for += away_score
            away.away_points_against += home_score

            # Win/Loss
            if home_score > away_score:
                home.ml_wins += 1
                away.ml_losses += 1
                home.recent_form.append("W")
                away.recent_form.append("L")
            elif away_score > home_score:
                away.ml_wins += 1
                home.ml_losses += 1
                away.recent_form.append("W")
                home.recent_form.append("L")
            else:
                home.recent_form.append("T")
                away.recent_form.append("T")

            # ATS: spread_line is from home team's perspective
            # nflverse: positive spread = home favored. Covers if margin > line.spread.
            if line and line.spread is not None:
                margin = home_score - away_score
                home_covers = margin > -line.spread  # negated: spread follows standard convention (negative=favored)
                away_covers = margin < -line.spread

                if home_covers == away_covers:
                    home.ats_pushes += 1; away.ats_pushes += 1
                # nflverse: positive spread = home favored, negative = home underdog
                elif home_covers:
                    home.ats_wins += 1; away.ats_losses += 1
                    if line.spread > 0:
                        away.underdog_wins += 1  # home favored, away is dog
                        away.underdog_games += 1
                    if True: pass  # nop for spacing
                else:
                    home.ats_losses += 1; away.ats_wins += 1
                    if line.spread < 0:
                        home.underdog_wins += 1  # home underdog
                        home.underdog_games += 1

            # O/U
            if line and line.over_under is not None:
                total = home_score + away_score
                if total > line.over_under:
                    home.over_hits += 1; away.over_hits += 1
                elif total < line.over_under:
                    home.under_hits += 1; away.under_hits += 1
                else:
                    home.over_under_pushes += 1; away.over_under_pushes += 1

        return all_stats


# ── Matchup Analysis ───────────────────────────────────────────────────

class MatchupAnalysis:
    """Analyzes a single game matchup and produces picks."""

    def __init__(self, game, home_abbr, away_abbr, home_stats, away_stats,
                 line=None, season_avg_pts=0.0, situation=None, split=None):
        self.game = game
        self.home_team_abbr = home_abbr
        self.away_team_abbr = away_abbr
        self.home_stats = home_stats
        self.away_stats = away_stats
        self.line = line
        self.season_avg_pts = season_avg_pts
        self.situation = situation  # GameSituation or None
        self.split = split  # BettingSplit or None

        self.margin_ats = None        # ATS-optimized model margin
        self.ou_total_v2 = None       # predicted total from OU model
        self.ou_conf_v2 = None        # confidence from OU model
        self.ml_home_prob = None      # ML model home win probability
        self.ml_edge = None           # edge over market-implied probability
        self.predicted_home = None
        self.predicted_away = None
        self.spread_pick = None
        self.spread_conf = None
        self.spread_reason = None
        self.ou_pick = None
        self.ou_conf = None
        self.ou_reason = None
        self.ml_pick = None
        self.ml_conf = None
        self.ml_reason = None
        # Contradiction tracking
        self.conflict_level = 0
        self.conflict_desc = []
        self.ats_ml_agree = None


    def predict(self):
        """Predict using pre-computed XGBoost model outputs.

        Raises:
            RuntimeError: If no trained margin model is available.
        """
        if self.home_stats.games < 1 or self.away_stats.games < 1:
            return

        # Require a trained ATS margin model — never fall back to heuristics
        if self.margin_ats is None:
            raise RuntimeError(
                f"No trained ATS margin model available for {self.away_team_abbr} @ {self.home_team_abbr}. "
                "Cannot make predictions without ML models."
            )

        margin = self.margin_ats

        # Total: prefer OU model, otherwise derive from margin + league average
        total = self.ou_total_v2 if self.ou_total_v2 is not None else self.season_avg_pts * 2

        self.predicted_home = round((total + margin) / 2, 1)
        self.predicted_away = round((total - margin) / 2, 1)

        # Derived win probability for conflict detection (uses model margin via logistic)
        home_win = max(min(1.0 / (1.0 + np.exp(-margin / 7.0)), 0.95), 0.05)

        # ── Spread pick (uses ATS-optimized margin) ──
        if self.line and self.line.spread is not None:
            eff = self.predicted_home - self.predicted_away - self.line.spread

            if eff > 0.5:
                display_spread = -self.line.spread
                self.spread_pick = f"{self.home_team_abbr} {display_spread:+.1f}"
                base_conf = min(0.5 + abs(margin) * 0.025, 0.80)
                self.spread_conf = round(min(base_conf + min(abs(eff) / 20, 0.15), 0.95), 2)
                self.spread_reason = self._spread_reason(True)
            elif eff < -0.5:
                display_spread = self.line.spread
                self.spread_pick = f"{self.away_team_abbr} {display_spread:+.1f}"
                base_conf = min(0.5 + abs(margin) * 0.025, 0.80)
                self.spread_conf = round(min(base_conf + min(abs(eff) / 20, 0.15), 0.95), 2)
                self.spread_reason = self._spread_reason(False)
            else:
                self.spread_pick = "Push / No value"
                self.spread_conf = 0.50

        # ── Moneyline pick (from predicted score, ML model adjusts confidence) ──
        # Direction comes from who the ATS+OU blended predicted score says will win.
        # ML model's win probability only affects confidence, not the pick direction.
        if margin > 0:
            self.ml_pick = f"{self.home_team_abbr} (+{self.line.home_moneyline:.0f})" if self.line and hasattr(self.line, 'home_moneyline') and self.line.home_moneyline else self.home_team_abbr
        elif margin < 0:
            self.ml_pick = f"{self.away_team_abbr} (+{self.line.away_moneyline:.0f})" if self.line and hasattr(self.line, 'away_moneyline') and self.line.away_moneyline else self.away_team_abbr

        if self.ml_pick and self.ml_home_prob is not None and self.line and hasattr(self.line, 'home_moneyline') and self.line.home_moneyline:
            def _impl(v):
                if v is None: return 0.5
                if v > 0: return round(100 / (v + 100) * 100, 1) / 100
                return round(abs(v) / (abs(v) + 100) * 100, 1) / 100
            ml_home = _impl(self.line.home_moneyline)
            ml_away = 1.0 - ml_home
            # ML confidence: how much does ML model agree with predicted winner?
            predicted_winner_prob = self.ml_home_prob if margin > 0 else (1 - self.ml_home_prob)
            market_implied = ml_home if margin > 0 else ml_away
            ml_edge = predicted_winner_prob - market_implied
            self.ml_conf = round(min(0.5 + abs(ml_edge), 0.90), 2)
            self.ml_reason = f"Win prob {predicted_winner_prob:.0%} vs implied {market_implied:.0%} (ML model)"

        # Check for contradictions between models
        self._detect_conflicts(margin, self.ml_home_prob or home_win, total)

        # ── Over/Under (from OU XGBoost model — no heuristic adjustments) ──
        if self.ou_total_v2 is not None and self.line and self.line.over_under is not None:
            diff = self.ou_total_v2 - self.line.over_under

            if diff > 1:
                self.ou_pick = "Over"
                self.ou_conf = round(0.5 + min(diff / 14, 0.45), 2)
                self.ou_reason = self._ou_reason(True, self.ou_total_v2)
            elif diff < -1:
                self.ou_pick = "Under"
                self.ou_conf = round(0.5 + min(abs(diff) / 14, 0.45), 2)
                self.ou_reason = self._ou_reason(False, self.ou_total_v2)
            else:
                self.ou_pick = "Push"
                self.ou_conf = 0.50
                self.ou_reason = f"Pred {self.ou_total_v2:.1f} vs line {self.line.over_under} | No edge"


    def _detect_conflicts(self, margin: float, home_win_prob: float, total: float):
        """Compare ATS, ML, and OU predictions for internal consistency."""
        self.conflict_level = 0
        self.conflict_desc = []
        self.ats_ml_agree = None

        ats_home_favored = margin > 0
        ml_home_favored = home_win_prob > 0.5
        self.ats_ml_agree = ats_home_favored == ml_home_favored

        if not self.ats_ml_agree and self.spread_pick and self.ml_pick:
            self.conflict_level = max(self.conflict_level, 2)
            self.conflict_desc.append(
                f"ATS favors {'Home' if ats_home_favored else 'Away'} ({margin:+.1f}) but ML favors {'Away' if ats_home_favored else 'Home'} ({home_win_prob:.0%})"
            )
            # Clear the ML pick when ATS and ML disagree — showing a contradictory
            # side pick (e.g. betting on the team predicted to lose) is misleading.
            self.ml_pick = None
            if self.spread_conf:
                self.spread_conf = round(self.spread_conf * 0.80, 2)
        elif self.ats_ml_agree and self.spread_pick and self.ml_pick:
            if self.spread_conf:
                self.spread_conf = round(min(self.spread_conf * 1.10, 0.95), 2)
            if self.ml_conf:
                self.ml_conf = round(min(self.ml_conf * 1.10, 0.95), 2)

        if (self.spread_pick and self.ou_pick and self.ou_pick != "Push"
                and self.line and self.line.over_under):
            ats_mag = abs(margin)
            ou_dev = abs(total - self.line.over_under)
            if ats_mag > 14 and ou_dev < 3:
                self.conflict_level = max(self.conflict_level, 1)
                self.conflict_desc.append(f"Large margin ({margin:+.1f}) but total near line ({total:.0f} vs {self.line.over_under:.1f})")

    def _spread_reason(self, home: bool) -> str:
        t = self.home_stats if home else self.away_stats
        o = self.away_stats if home else self.home_stats
        team = self.home_team_abbr if home else self.away_team_abbr
        parts = [f"{team} scores {t.ppg_for} PPG, allows {t.ppg_against} PPG"]
        parts.append(f"Opp scores {o.ppg_for} PPG, allows {o.ppg_against} PPG")
        if t.ats_pct is not None:
            parts.append(f"{team} {t.ats_pct:.0%} ATS")
        if t.recent_form_str != "N/A":
            parts.append(f"Form: {t.recent_form_str}")
        # Situational factors
        if self.situation:
            sit_parts = []
            if self.situation.is_division:
                sit_parts.append("division")
            if self.situation.is_short_week:
                sit_parts.append("short wk")
            if self.situation.home_off_bye:
                sit_parts.append("H bye")
            if self.situation.away_off_bye:
                sit_parts.append("A bye")
            if self.situation.rest_differential and abs(self.situation.rest_differential) >= 3:
                sit_parts.append(f"rest {'+' if self.situation.rest_differential > 0 else ''}{self.situation.rest_differential:.0f}d")
            if self.situation.travel_advantage != "neutral":
                sit_parts.append(f"travel:{self.situation.travel_advantage}")
            if sit_parts:
                parts.append(f"Situ: {', '.join(sit_parts)} ({self.situation.situation_score:+.1f})")
        # Line movement
        if self.split and self.split.spread_movement is not None:
            parts.append(f"Line: open {self.split.opening_spread:+.1f} → current {self.split.current_spread:+.1f} ({self.split.spread_movement:+.1f})")
            if self.split.home_side_pct is not None:
                parts.append(f"Pub: {self.split.home_side_pct:.0f}% on H")
        return " | ".join(parts)

    def _ou_reason(self, over: bool, predicted: float) -> str:
        h = self.home_stats; a = self.away_stats
        return (f"Pred {predicted:.1f} vs line {self.line.over_under} | "
                f"{self.home_team_abbr} games avg {h.ppg_for + h.ppg_against:.1f} | "
                f"{self.away_team_abbr} games avg {a.ppg_for + a.ppg_against:.1f}")

    def to_pick_card(self) -> dict:
        t = self.game["date"].strftime("%Y-%m-%d %H:%M UTC") if self.game["date"] else "TBD"
        card = {
            "game": f"{self.away_team_abbr} @ {self.home_team_abbr}",
            "game_time": t,
            "week": self.game["week"],
            "season": self.home_stats.year,
            "team_stats": {"home": self.home_stats.to_dict(), "away": self.away_stats.to_dict()},
            "situational": self.situation.to_dict() if self.situation else None,
            "betting_splits": self.split.to_dict() if self.split else None,
            "market": {},
            "predictions": {},
        }
        if self.line:
            def _imp(v):
                if v is None: return None
                if v > 0: return round(100 / (v + 100) * 100, 1) / 100
                return round(abs(v) / (abs(v) + 100) * 100, 1) / 100
            card["market"] = {
                "spread": self.line.spread,
                "over_under": self.line.over_under,
                "home_moneyline": self.line.home_moneyline,
                "away_moneyline": self.line.away_moneyline,
                "home_implied_prob": _imp(getattr(self.line, 'home_moneyline', None)),
                "away_implied_prob": _imp(getattr(self.line, 'away_moneyline', None)),
            }
            if self.ou_total_v2 is not None or self.margin_ats is not None or self.ml_home_prob is not None:
                card["model_info"] = {
                    "ats_model_margin": self.margin_ats,
                    "ou_model_total": self.ou_total_v2,
                    "ml_home_prob": self.ml_home_prob,
                    "ml_edge": self.ml_edge,
                }
        preds = {}
        if self.predicted_home is not None:
            preds["predicted_score"] = {
                "blended": {
                    "home": self.predicted_home,
                    "away": self.predicted_away,
                    "total": round(self.predicted_home + self.predicted_away, 1),
                    "margin": round(self.predicted_home - self.predicted_away, 1),
                },
            }
            preds["spread"] = {
                "pick": self.spread_pick, "confidence": self.spread_conf,
                "reasoning": self.spread_reason,
                "model": "ATS"
            }
        if self.ou_pick:
            preds["over_under"] = {
                "pick": self.ou_pick, "confidence": self.ou_conf,
                "reasoning": self.ou_reason,
                "model": "OU"
            }
        if self.ml_pick:
            preds["moneyline"] = {
                "pick": self.ml_pick, "confidence": self.ml_conf,
                "reasoning": self.ml_reason,
                "model": "ML"
            }
        # Model conflict analysis
        if self.conflict_level > 0:
            card["conflict"] = {
                "level": self.conflict_level,
                "details": self.conflict_desc,
                "models_agree": self.ats_ml_agree,
            }
        else:
            card["conflict"] = {"level": 0, "details": [], "models_agree": True}

        card["predictions"] = preds
        return card


# ── Handicapper ────────────────────────────────────────────────────────

class Handicapper:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.builder = TeamStatsBuilder(db)

    async def handicap_week(self, year: int, week: int, num_games_analysis: Optional[int] = None,
                             league_average: Optional[float] = None, for_week: Optional[int] = None) -> list[dict]:
        """
        Produce picks for a week's games.

        Args:
            for_week: If set, build stats only from games before this week (backtesting).
                       Defaults to week (predicting week N using games before N).
        """
        # When backtesting: build stats from games before the target week
        max_w = for_week if for_week is not None else None
        all_stats = await self.builder.build(year, num_games=num_games_analysis, max_week=max_w)

        if not all_stats:
            logger.warning(f"No team stats for {year}")
            return []

        if league_average is None:
            total_pts = sum(s.points_for + s.points_against for s in all_stats.values())
            total_games = sum(s.games for s in all_stats.values()) * 2
            league_average = round(total_pts / max(total_games, 1), 1)

        season_r = await self.db.execute(select(Season).where(Season.year == year))
        season = season_r.scalar_one_or_none()
        if not season:
            return []

        game_r = await self.db.execute(
            select(Game).where(
                Game.season_id == season.id,
                Game.week == week,
                Game.game_type == "REG",
            ).order_by(Game.date))
        games_raw = game_r.scalars().all()
        if not games_raw:
            return []

        # Extract ORM data into lightweight dicts immediately to avoid
        # lazy-load issues after session operations (flush/rollback).
        games = [
            {
                "id": g.id,
                "week": g.week,
                "season_id": g.season_id,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "date": g.date,
                "game_type": g.game_type,
                "home_score": g.home_score,
                "away_score": g.away_score,
            }
            for g in games_raw
        ]

        team_r = await self.db.execute(select(Team))
        teams = {t.id: t.abbreviation for t in team_r.scalars().all()}

        lines_r = await self.db.execute(
            select(GameLines).where(GameLines.game_id.in_([g["id"] for g in games])))
        lines_raw = {gl.game_id: gl for gl in lines_r.scalars().all()}
        # Extract ORM data into lightweight SimpleNamespace objects to avoid
        # lazy-load issues after session operations (flush/rollback).
        from types import SimpleNamespace
        lines = {}
        for gid, gl in lines_raw.items():
            lines[gid] = SimpleNamespace(
                game_id=gid,
                spread=gl.spread,
                over_under=gl.over_under,
                opening_spread=gl.opening_spread,
                opening_ou=gl.opening_ou,
                home_moneyline=gl.home_moneyline,
                away_moneyline=gl.away_moneyline,
                source_opening=gl.source_opening,
                source_closing=gl.source_closing,
            )

        # Load situational context for all games
        sit_analyzer = SituationalAnalyzer(self.db)
        split_analyzer = SplitAnalyzer(self.db)
        season_r2 = await self.db.execute(select(Season).where(Season.year == year))
        season2 = season_r2.scalar_one_or_none()
        sit_contexts = {}
        split_contexts = {}
        if season2:
            game_ids = [g["id"] for g in games]
            for gid in game_ids:
                try:
                    ctx = await sit_analyzer.analyze_game(gid)
                    if ctx:
                        sit_contexts[gid] = ctx
                except Exception as e:
                    pass
                try:
                    sp = await split_analyzer.analyze_game(gid)
                    if sp and sp.spread_movement is not None:
                        split_contexts[gid] = sp
                except Exception as e:
                    pass

        cards = []
        for g in games:
            ha = teams.get(g["home_team_id"], ""); aa = teams.get(g["away_team_id"], "")
            hs = all_stats.get(ha); aws = all_stats.get(aa)
            if not hs or not aws:
                continue
            gid = g["id"]
            gwk = g["week"]
            gl = lines.get(gid)
            # ── Run all three specialized models ──
            # 1. ATS model → margin prediction (spread-optimized)
            try:
                ats_margin, _ = await xgb_predict_margin_ats(
                    self.db, gid, ha, aa, year, gwk,
                    hs, aws, gl, league_average
                )
            except Exception as e:
                logger.warning(f"ATS model failed for game {gid}: {e}")
                ats_margin = None


            # 3. OU model → total points
            try:
                ou_total, ou_conf = await xgb_predict_ou_total(
                    self.db, gid, ha, aa, year, gwk,
                    hs, aws, gl, league_average
                )
            except Exception as e:
                logger.warning(f"OU prediction failed for game {gid}: {e}")
                ou_total, ou_conf = None, None

            # 4. ML model → home win probability
            try:
                ml_prob, ml_conf, ml_edge = await xgb_predict_ml_prob(
                    self.db, gid, ha, aa, year, gwk,
                    hs, aws, gl, league_average
                )
            except Exception as e:
                logger.warning(f"ML model failed for game {gid}: {e}")
                ml_prob, ml_conf, ml_edge = None, None, None

            a = MatchupAnalysis(g, ha, aa, hs, aws, gl, league_average,
                                situation=sit_contexts.get(gid),
                                split=split_contexts.get(gid))
            a.margin_ats = ats_margin
            a.ou_total_v2 = ou_total
            a.ou_conf_v2 = ou_conf
            a.ml_home_prob = ml_prob
            a.ml_edge = ml_edge

            a.predict()
            cards.append(a.to_pick_card())

            # Save prediction to database
            try:
                await self._save_prediction(a, year)
            except Exception as e:
                logger.warning(f"Could not save prediction for game {gid}: {e}")

        return cards

    async def _save_prediction(self, a, year: int):
        """Save a MatchupAnalysis prediction to game_predictions table."""
        from sqlalchemy import select
        from datetime import datetime, timezone

        if a.predicted_home is None:
            return

        predicted_total = round(a.predicted_home + a.predicted_away, 1)
        predicted_margin = round(a.predicted_home - a.predicted_away, 1)

        ou_pick = a.ou_pick if a.ou_pick else None

        # Check if prediction already exists for this game
        existing = await self.db.execute(
            select(GamePrediction).where(
                GamePrediction.game_id == a.game["id"],
                GamePrediction.source == "api",
            )
        )
        if existing.scalar_one_or_none():
            return

        # Serialize enriched metadata
        import json
        gp = GamePrediction(
            game_id=a.game["id"],
            predicted_home_score=round(a.predicted_home, 1),
            predicted_away_score=round(a.predicted_away, 1),
            predicted_total=predicted_total,
            predicted_margin=predicted_margin,
            margin_conf=max(
                a.spread_conf or 0,
                a.ml_conf or 0,
                a.ou_conf_v2 or 0,
            ) if any([a.spread_conf, a.ml_conf, a.ou_conf_v2]) else None,
            ou_pick=ou_pick,
            spread_pick=a.spread_pick,
            source="api",
            home_stats_json=json.dumps(a.home_stats.to_dict()) if a.home_stats else None,
            away_stats_json=json.dumps(a.away_stats.to_dict()) if a.away_stats else None,
            situational_json=json.dumps(a.situation.to_dict()) if a.situation else None,
            splits_json=json.dumps(a.split.to_dict()) if a.split else None,
        )

        # Fill in actuals if game has been played
        from app.models import Game as GameModel
        game_r = await self.db.execute(
            select(GameModel).where(GameModel.id == a.game["id"])
        )
        game = game_r.scalar_one_or_none()
        if game and game.home_score is not None:
            hs, aws = int(game.home_score), int(game.away_score)
            gp.actual_home_score = hs
            gp.actual_away_score = aws
            gp.actual_total = hs + aws
            gp.actual_margin = hs - aws

            # Compute ATS result
            if a.line and a.line.spread is not None:
                sp = a.line.spread
                am = hs - aws
                if abs(am + sp) < 0.5:
                    gp.ats_result = "Push"
                elif (predicted_margin > -sp) == (am > -sp):
                    gp.ats_result = "Win"
                else:
                    gp.ats_result = "Loss"

            # Compute O/U result
            if a.line and a.line.over_under is not None:
                actual_total = hs + aws
                vegas_ou = a.line.over_under
                if abs(actual_total - vegas_ou) < 0.5:
                    gp.ou_result = "Push"
                elif (predicted_total > vegas_ou) == (actual_total > vegas_ou):
                    gp.ou_result = "Win"
                else:
                    gp.ou_result = "Loss"

            # Compute ML result
            if am != 0:
                gp.ml_result = "Win" if (predicted_margin > 0) == (am > 0) else "Loss"

            # ── PnL calculation (flat $100 risk per bet) ──
            def _pl(result, odds):
                if result == "Win":
                    return round(100 * (100.0 / abs(odds) if odds < 0 else odds / 100.0), 2)
                elif result == "Loss":
                    return -100.0
                return 0.0

            # ATS: -110 standard juice
            gp.ats_odds = -110
            gp.ats_profit = _pl(gp.ats_result, -110) if gp.ats_result else None

            # OU: -110 standard juice
            gp.ou_odds = -110
            gp.ou_profit = _pl(gp.ou_result, -110) if gp.ou_result else None

            # ML: actual moneyline odds of the picked team
            if am != 0:
                ml_pick_home = predicted_margin > 0
                hml = getattr(a.line, 'home_moneyline', None) if a.line else None
                aml = getattr(a.line, 'away_moneyline', None) if a.line else None
                gp.ml_odds = hml if ml_pick_home else aml
                gp.ml_profit = _pl(gp.ml_result, gp.ml_odds) if gp.ml_odds else None

        from sqlalchemy import exc as _sa_exc
        self.db.add(gp)
        try:
            await self.db.flush()
        except _sa_exc.IntegrityError:
            # Duplicate — skip silently, another request already saved this
            await self.db.rollback()

    async def analyze_matchup(self, home_team_abbr: str, away_team_abbr: str,
                                year: int, week: int, num_games_analysis: Optional[int] = 5) -> Optional[dict]:
        cards = await self.handicap_week(year, week, num_games_analysis)
        gs = f"{away_team_abbr.upper()} @ {home_team_abbr.upper()}"
        for c in cards:
            if c["game"].upper() == gs.upper():
                return c
        return None


# ── Backtest Runner ───────────────────────────────────────────────────

async def backtest_season(db: AsyncSession, year: int,
                          end_week: Optional[int] = None,
                          num_games: Optional[int] = 3) -> dict:
    """
    Run a week-by-week backtest of the handicapping engine, always from week 1.

    For each week N:
      1. Build team stats from games in weeks 1 through N-1 only
      2. Make predictions for week N games
      3. Compare against actual results

    Returns aggregated accuracy stats.
    """
    start_week = 1
    handicapper = Handicapper(db)

    # Find the season
    season_r = await db.execute(select(Season).where(Season.year == year))
    season = season_r.scalar_one_or_none()
    if not season:
        return {"error": f"Season {year} not found"}

    # Clear any existing api predictions for this year
    await db.execute(
        delete(GamePrediction).where(
            GamePrediction.game_id.in_(
                select(Game.id).where(Game.season_id == season.id)
            ),
            GamePrediction.source == "api",
        )
    )
    await db.commit()

    # Load year-specific models for backtesting
    import os
    from app.handicapping.nfl.nfl_xgb_model_ou import set_model_path as set_ou_model
    from app.handicapping.nfl.nfl_xgb_model_ats import set_model_path as set_ats_model
    from app.handicapping.nfl.nfl_xgb_model_ml import set_model_path as set_ml_model

    ou_path = f"/app/data/ou_model_{year}.pkl"
    ats_path = f"/app/data/handicap_model_ats_{year}.pkl"
    ml_path = f"/app/data/handicap_model_ml_{year}.pkl"

    if os.path.exists(ou_path):
        set_ou_model(ou_path)
    if os.path.exists(ats_path):
        set_ats_model(ats_path)
    if os.path.exists(ml_path):
        set_ml_model(ml_path)

    # Find max week
    max_r = await db.execute(
        select(Game).where(
            Game.season_id == season.id, Game.game_type == "REG",
            Game.home_score.isnot(None)
        ).order_by(Game.week.desc()).limit(1)
    )
    max_g = max_r.scalar_one_or_none()
    if not max_g:
        return {"error": f"No games found for {year}"}
    if end_week is None:
        end_week = max_g.week

    # Team abbreviation map
    team_r = await db.execute(select(Team))
    teams = {t.id: t.abbreviation for t in team_r.scalars().all()}

    # Pre-load all games for result comparison (raw SQL avoids ORM lazy-load)
    all_games_r = await db.execute(
        text("""
            SELECT g.week, ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score, g.id
            FROM nfl.games g
            JOIN nfl.seasons s ON s.id=g.season_id
            JOIN nfl.teams ht ON ht.id=g.home_team_id
            JOIN nfl.teams at ON at.id=g.away_team_id
            WHERE s.year = :year AND g.game_type='REG'
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        """),
        {"year": year}
    )
    games_by_key = {}
    for r in all_games_r.fetchall():
        games_by_key[(r.week, r.ha, r.aa)] = r

    db_results = {
        "season": year,
        "weeks_tested": 0,
        "total_games": 0,
        "ats": {"correct": 0, "incorrect": 0, "pushes": 0, "pct": 0.0},
        "over_under": {"correct": 0, "incorrect": 0, "pushes": 0, "pct": 0.0},
        "moneyline": {"correct": 0, "incorrect": 0, "pct": 0.0},
        "ats_by_confidence": {},
        "week_results": [],
    }

    for wk in range(start_week, end_week + 1):
        cards = await handicapper.handicap_week(year, wk, num_games, for_week=wk)
        if not cards:
            continue

        db_results["weeks_tested"] += 1

        # Collect game IDs from this week's cards
        game_ids = []
        for card in cards:
            away_abbr, home_abbr = card["game"].split(" @ ")
            game_key = (card["week"], home_abbr, away_abbr)
            actual = games_by_key.get(game_key)
            if actual:
                game_ids.append(actual.id)

        if not game_ids:
            continue

        # Read results from DB predictions (computed with correct prediction-time line)
        pred_r = await db.execute(
            select(GamePrediction).where(
                GamePrediction.game_id.in_(game_ids),
                GamePrediction.source == "api",
            )
        )
        preds = pred_r.scalars().all()

        week_games = 0
        week_ats = {"correct": 0, "incorrect": 0, "pushes": 0}
        week_ou = {"correct": 0, "incorrect": 0, "pushes": 0}
        week_ml = {"correct": 0, "incorrect": 0}

        for p in preds:
            week_games += 1
            db_results["total_games"] += 1

            if p.ats_result == "Win":
                db_results["ats"]["correct"] += 1; week_ats["correct"] += 1
            elif p.ats_result == "Loss":
                db_results["ats"]["incorrect"] += 1; week_ats["incorrect"] += 1
            elif p.ats_result == "Push":
                db_results["ats"]["pushes"] += 1; week_ats["pushes"] += 1

            if p.ou_result == "Win":
                db_results["over_under"]["correct"] += 1; week_ou["correct"] += 1
            elif p.ou_result == "Loss":
                db_results["over_under"]["incorrect"] += 1; week_ou["incorrect"] += 1
            elif p.ou_result == "Push":
                db_results["over_under"]["pushes"] += 1; week_ou["pushes"] += 1

            if p.ml_result == "Win":
                db_results["moneyline"]["correct"] += 1; week_ml["correct"] += 1
            elif p.ml_result == "Loss":
                db_results["moneyline"]["incorrect"] += 1; week_ml["incorrect"] += 1

            # Confidence bracket (match card by game_id substring)
            if p.spread_pick:
                for card in cards:
                    if str(p.game_id) in card.get("game", ""):
                        sp_c = card.get("predictions", {}).get("spread", {}).get("confidence", 0.5)
                        cb = round(sp_c * 2, 0) / 2
                        ck = f"{cb:.2f}"
                        if ck not in db_results["ats_by_confidence"]:
                            db_results["ats_by_confidence"][ck] = {"correct": 0, "incorrect": 0, "pushes": 0}
                        r = p.ats_result
                        if r == "Win": db_results["ats_by_confidence"][ck]["correct"] += 1
                        elif r == "Loss": db_results["ats_by_confidence"][ck]["incorrect"] += 1
                        elif r == "Push": db_results["ats_by_confidence"][ck]["pushes"] += 1
                        break

        db_results["week_results"].append({
            "week": wk, "games": week_games,
            "ats": week_ats, "over_under": week_ou, "moneyline": week_ml,
        })

        if wk % 4 == 0:
            logger.info(f"  Backtest week {wk}: {week_games} games -- "
                        f"ATS {week_ats['correct']}/{week_ats['correct']+week_ats['incorrect']}")

    # Pct calc
    ats_t = db_results["ats"]["correct"] + db_results["ats"]["incorrect"]
    db_results["ats"]["pct"] = round(db_results["ats"]["correct"] / max(ats_t, 1), 3)
    ou_t = db_results["over_under"]["correct"] + db_results["over_under"]["incorrect"]
    db_results["over_under"]["pct"] = round(db_results["over_under"]["correct"] / max(ou_t, 1), 3)
    ml_t = db_results["moneyline"]["correct"] + db_results["moneyline"]["incorrect"]
    db_results["moneyline"]["pct"] = round(db_results["moneyline"]["correct"] / max(ml_t, 1), 3)

    for k, v in sorted(db_results["ats_by_confidence"].items()):
        t = v["correct"] + v["incorrect"]
        v["pct"] = round(v["correct"] / max(t, 1), 3)
        v["count"] = t

    logger.info(f"\nBacktest {year} complete!")
    logger.info(f"  ATS: {db_results['ats']['correct']}/{ats_t} = {db_results['ats']['pct']:.1%}")
    logger.info(f"  O/U: {db_results['over_under']['correct']}/{ou_t} = {db_results['over_under']['pct']:.1%}")
    logger.info(f"  ML:  {db_results['moneyline']['correct']}/{ml_t} = {db_results['moneyline']['pct']:.1%}")

    await db.commit()
    return db_results

