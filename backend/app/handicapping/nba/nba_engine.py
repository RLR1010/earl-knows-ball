"""
NBA handicapping engine — team stats, daily pick cards, matchup analysis.

Generates structured pick cards using XGBoost model predictions for NBA.
Raises RuntimeError if no trained model is available — no heuristic fallback.

Architecture mirrors MLB engine pattern:
  - NBAHandicapper loads 3 models (ATS/OU/ML) and predicts margin, total, win prob
  - NBAPickCard combines them with conflict detection → final score → picks
  - backtest_season() runs historical evaluation

NBA-specific: features include points (not runs), rest/back-to-back is critical,
shooting efficiency matters, pace influences totals.
"""
import logging
import os
import pickle
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from sqlalchemy import select, text as _sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBAGame, NBATeam, NBASeason

logger = logging.getLogger("earl.nba_handicapping")

from app.handicapping.nba.nba_xgb_model_ats import (
    predict_ats, set_model_path as set_ats_path, train_model as train_ats,
)
from app.handicapping.nba.nba_xgb_model_ou import (
    predict_ou, set_model_path as set_ou_path, train_model as train_ou,
)
from app.handicapping.nba.nba_xgb_model_ml import (
    predict_ml, set_model_path as set_ml_path, train_model as train_ml,
)

# NBA odds availability: consolidated table (nba.betting_lines_consolidated)
# has ~7,669 verified games from 2020-2025 only. No historical datasets exist.
# Years 2006-2019 have game scores but no betting lines.


CURRENT_YEAR = 2026


# ═══════════════════════════════════════════════════════════════════
# NBA Team Stats
# ═══════════════════════════════════════════════════════════════════

class NBATeamStats:
    """Per-team aggregated offensive and defensive stats."""

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
        self.over_hits = 0
        self.under_hits = 0
        self.ml_wins = 0
        self.ml_losses = 0
        self.home_points_for = 0.0
        self.home_points_against = 0.0
        self.away_points_for = 0.0
        self.away_points_against = 0.0
        self.recent_form = []
        self.last_10_wins = 0
        self.last_10_losses = 0

    @property
    def ppg_for(self) -> float:
        return round(self.points_for / max(self.games, 1), 2)

    @property
    def ppg_against(self) -> float:
        return round(self.points_against / max(self.games, 1), 2)

    @property
    def point_margin(self) -> float:
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
    def win_pct(self) -> Optional[float]:
        total = self.ml_wins + self.ml_losses
        return round(self.ml_wins / max(total, 1), 3) if total > 0 else None

    @property
    def ats_pct(self) -> Optional[float]:
        total = self.ats_wins + self.ats_losses
        return round(self.ats_wins / max(total, 1), 3) if total > 0 else None

    @property
    def over_pct(self) -> Optional[float]:
        total = self.over_hits + self.under_hits
        return round(self.over_hits / max(total, 1), 3) if total > 0 else None

    @property
    def recent_form_str(self) -> str:
        return "-".join(self.recent_form[-10:]) if self.recent_form else "N/A"

    def to_dict(self) -> dict:
        return {
            "team": self.team_abbr, "year": self.year, "games": self.games,
            "ppg_for": self.ppg_for, "ppg_against": self.ppg_against,
            "point_margin": self.point_margin,
            "home_ppg_for": self.home_ppg_for, "home_ppg_against": self.home_ppg_against,
            "away_ppg_for": self.away_ppg_for, "away_ppg_against": self.away_ppg_against,
            "win_pct": self.win_pct, "ats_pct": self.ats_pct, "over_pct": self.over_pct,
            "recent_form": self.recent_form_str,
            "last_10_wins": self.last_10_wins, "last_10_losses": self.last_10_losses,
        }


class NBATeamStatsBuilder:
    """Builds team strength metrics from game scores."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        year: int,
        num_games: Optional[int] = None,
        up_to_date: Optional[date] = None,
    ) -> dict[str, NBATeamStats]:
        season_r = await self.db.execute(
            select(NBASeason).where(NBASeason.year == year)
        )
        season = season_r.scalar_one_or_none()
        if not season:
            return {}

        conditions = [
            NBAGame.season_id == season.id,
            NBAGame.home_score.isnot(None),
            NBAGame.away_score.isnot(None),
        ]
        if up_to_date:
            conditions.append(NBAGame.date < datetime.combine(up_to_date, datetime.min.time(), tzinfo=timezone.utc))

        r = await self.db.execute(
            select(NBAGame).where(*conditions).order_by(NBAGame.date)
        )
        all_games = r.scalars().all()
        if not all_games:
            return {}

        tr = await self.db.execute(select(NBATeam))
        teams = {t.id: t.abbreviation for t in tr.scalars().all()}

        all_stats: dict[str, NBATeamStats] = {}
        for tid, tabbr in teams.items():
            all_stats[tabbr] = NBATeamStats(tabbr, tid, year)

        if num_games:
            sorted_by_date = sorted(
                all_games,
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
            games_to_use = all_games

        for g in games_to_use:
            home_abbr = teams.get(g.home_team_id, "")
            away_abbr = teams.get(g.away_team_id, "")
            if not home_abbr or not away_abbr:
                continue
            home = all_stats[home_abbr]
            away = all_stats[away_abbr]
            home_score = g.home_score or 0
            away_score = g.away_score or 0
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
            if home_score > away_score:
                home.ml_wins += 1
                away.ml_losses += 1
                home.recent_form.append("W")
                away.recent_form.append("L")
            else:
                home.ml_losses += 1
                away.ml_wins += 1
                home.recent_form.append("L")
                away.recent_form.append("W")

        for abbr, stats in all_stats.items():
            last10 = [r for r in stats.recent_form[-10:]]
            stats.last_10_wins = last10.count("W")
            stats.last_10_losses = last10.count("L")

        return all_stats

    async def build_for_game(
        self,
        game_id: int,
        num_games: int = 10,
    ) -> tuple[Optional[dict[str, NBATeamStats]], Optional[int], Optional[date], Optional[str], Optional[str]]:
        """Build team stats for just the two teams in a specific game."""
        gr = await self.db.execute(select(NBAGame).where(NBAGame.id == game_id))
        game = gr.scalar_one_or_none()
        if not game or not game.home_team_id or not game.away_team_id or not game.date:
            return None, None, None, None, None

        sr = await self.db.execute(select(NBASeason).where(NBASeason.id == game.season_id))
        season = sr.scalar_one_or_none()
        if not season:
            return None, None, None, None, None
        season_year = season.year

        tr = await self.db.execute(select(NBATeam).where(
            NBATeam.id.in_([game.home_team_id, game.away_team_id])
        ))
        team_map = {t.id: t.abbreviation for t in tr.scalars().all()}
        home_abbr = team_map.get(game.home_team_id)
        away_abbr = team_map.get(game.away_team_id)
        if not home_abbr or not away_abbr:
            return None, None, None, None, None

        _gd = game.date
        if isinstance(_gd, datetime):
            game_date = _gd.date()
        else:
            game_date = _gd

        cutoff = datetime.combine(game_date, datetime.min.time(), tzinfo=timezone.utc)

        conditions = [
            NBAGame.season_id == game.season_id,
            NBAGame.home_score.isnot(None),
            NBAGame.away_score.isnot(None),
            NBAGame.date < cutoff,
            ((NBAGame.home_team_id == game.home_team_id) |
             (NBAGame.away_team_id == game.home_team_id) |
             (NBAGame.home_team_id == game.away_team_id) |
             (NBAGame.away_team_id == game.away_team_id)),
        ]
        r = await self.db.execute(
            select(NBAGame).where(*conditions).order_by(NBAGame.date.desc())
        )
        past_games = r.scalars().all()

        all_stats: dict[str, NBATeamStats] = {}
        for abbr in [home_abbr, away_abbr]:
            tid = game.home_team_id if abbr == home_abbr else game.away_team_id
            all_stats[abbr] = NBATeamStats(abbr, tid, season_year)

        team_count: dict[str, int] = defaultdict(int)
        for g in past_games:
            ha = team_map.get(g.home_team_id, "")
            aa = team_map.get(g.away_team_id, "")
            if ha not in all_stats and aa not in all_stats:
                continue
            if team_count[ha] >= num_games and team_count[aa] >= num_games:
                continue
            team_count[ha] += 1
            team_count[aa] += 1

            home = all_stats.get(ha)
            away = all_stats.get(aa)
            if not home or not away:
                continue

            hscore = g.home_score or 0
            ascore = g.away_score or 0

            home.games += 1
            away.games += 1
            if g.home_team_id == home.team_id:
                home.home_games += 1
            if g.away_team_id == away.team_id:
                away.away_games += 1

            home.points_for += hscore
            home.points_against += ascore
            away.points_for += ascore
            away.points_against += hscore

            if hscore > ascore:
                home.ml_wins += 1
                away.ml_losses += 1
                home.recent_form.append("W")
                away.recent_form.append("L")
            else:
                home.ml_losses += 1
                away.ml_wins += 1
                home.recent_form.append("L")
                away.recent_form.append("W")

        for stats in all_stats.values():
            last10 = [r for r in stats.recent_form[-10:]]
            stats.last_10_wins = last10.count("W")
            stats.last_10_losses = last10.count("L")

        return all_stats, season_year, game_date, home_abbr, away_abbr


# ═══════════════════════════════════════════════════════════════════
# NBA Matchup Analysis (Pick Card)
# ═══════════════════════════════════════════════════════════════════

class NBAPickCard:
    """Handicapping analysis for a single NBA game, driven by XGBoost models.

    Mirrors MLBPickCard: combines ATS margin + OU total + ML win prob
    into a predicted score, then generates picks with conflict-adjusted confidence.
    """

    def __init__(self, game: NBAGame, home_abbr: str, away_abbr: str,
                 season_avg_total: float = 220.0):
        self.game_id = game.id
        self.home_team = home_abbr
        self.away_team = away_abbr
        self.game_time = game.date.isoformat() if game.date else None
        self.season_avg_total = season_avg_total

        # Market lines
        self.home_moneyline: Optional[int] = None
        self.away_moneyline: Optional[int] = None
        self.spread: Optional[float] = None
        self.over_under: Optional[float] = None

        # Three model predictions (set by NBAHandicapper before predict())
        self.margin_ats: Optional[float] = None     # from ATS model
        self.ou_total: Optional[float] = None        # from OU model
        self.home_win_prob: Optional[float] = None   # from ML model
        self.ml_edge: float = 0.0                    # edge over market implied

        # Derived final score (from margin + total)
        self.predicted_home_score: Optional[float] = None
        self.predicted_away_score: Optional[float] = None
        self.predicted_total: Optional[float] = None
        self.predicted_margin: Optional[float] = None

        # Picks
        self.moneyline_pick: Optional[str] = None
        self.ml_confidence: float = 0.0
        self.spread_pick: Optional[str] = None
        self.spread_confidence: float = 0.0
        self.over_under_pick: Optional[str] = None
        self.ou_confidence: float = 0.0

        # Conflict tracking
        self.ats_ml_agree: Optional[bool] = None
        self.conflict_level: int = 0
        self.conflict_desc: list[str] = []

        self.reasoning: list[str] = []
        self.home_stats: Optional[dict] = None
        self.away_stats: Optional[dict] = None

    def predict(self):
        """Combine ATS margin + OU total + ML win prob into final score + picks.

        Requires all three XGBoost models. Raises RuntimeError if any model
        output is missing — no heuristic fallbacks.
        """
        if self.margin_ats is None:
            raise RuntimeError(
                f"No ATS model margin for {self.away_team} @ {self.home_team}. "
                "Cannot make predictions without all three models."
            )
        if self.ou_total is None:
            raise RuntimeError(
                f"No OU model total for {self.away_team} @ {self.home_team}. "
                "Cannot make predictions without all three models."
            )
        if self.home_win_prob is None:
            raise RuntimeError(
                f"No ML model probability for {self.away_team} @ {self.home_team}. "
                "Cannot make predictions without all three models."
            )

        total = self.ou_total
        margin = self.margin_ats

        # Predicted score
        self.predicted_home_score = round((total + margin) / 2, 2)
        self.predicted_away_score = round((total - margin) / 2, 2)
        self.predicted_total = round(self.predicted_home_score + self.predicted_away_score, 2)
        self.predicted_margin = round(margin, 2)

        # ── Spread pick ──
        # Spread convention: negative = home favored, positive = home underdog
        # Home covers if actual_margin > spread (e.g., spread=-5, home wins by 6 → covers)
        if self.margin_ats is not None and self.spread is not None:
            # The margin by which the home team covers the spread
            margin_vs_spread = margin - self.spread
            if margin_vs_spread > 0:
                self.spread_pick = self.home_team
                self.spread_confidence = round(min(0.5 + abs(margin_vs_spread) * 0.06, 0.90), 2)
            else:
                self.spread_pick = self.away_team
                self.spread_confidence = round(min(0.5 + abs(margin_vs_spread) * 0.06, 0.90), 2)

        # ── Moneyline pick ──
        if margin > 0:
            self.moneyline_pick = "home"
        elif margin < 0:
            self.moneyline_pick = "away"

        if self.moneyline_pick and self.home_moneyline is not None and self.home_win_prob is not None:
            def _impl(v):
                if v is None:
                    return 0.5
                return (abs(v) / (abs(v) + 100)) if v < 0 else (100 / (v + 100))
            ml_home = _impl(self.home_moneyline)
            ml_away = 1.0 - ml_home
            predicted_winner_prob = self.home_win_prob if self.moneyline_pick == "home" else (1 - self.home_win_prob)
            market_implied = ml_home if self.moneyline_pick == "home" else ml_away
            ml_edge = predicted_winner_prob - market_implied
            self.ml_edge = ml_edge
            self.ml_confidence = round(min(0.5 + abs(ml_edge), 0.90), 2)

        # ── Over/Under pick ──
        if self.ou_total is not None and self.over_under is not None:
            diff = self.ou_total - self.over_under
            if diff > 0:
                self.over_under_pick = "over"
                self.ou_confidence = round(min(0.5 + abs(diff) * 0.03, 0.90), 2)
            else:
                self.over_under_pick = "under"
                self.ou_confidence = round(min(0.5 + abs(diff) * 0.03, 0.90), 2)

        # ── Conflict detection ──
        self._detect_conflicts(margin)

    def _detect_conflicts(self, margin: float):
        """Check ATS vs ML model agreement."""
        self.conflict_level = 0
        self.conflict_desc = []
        self.ats_ml_agree = None

        if self.home_win_prob is not None:
            ats_home_favored = margin > 0
            ml_home_favored = self.home_win_prob > 0.5
            self.ats_ml_agree = ats_home_favored == ml_home_favored

            if not self.ats_ml_agree and self.spread_pick:
                self.conflict_level = max(self.conflict_level, 2)
                self.conflict_desc.append(
                    f"ATS favors {'Home' if ats_home_favored else 'Away'} ({margin:+.1f}) "
                    f"but ML model gives home {self.home_win_prob:.0%} (favors {'Away' if ats_home_favored else 'Home'})"
                )
                if self.spread_confidence:
                    self.spread_confidence = round(self.spread_confidence * 0.80, 2)
                if self.ml_confidence:
                    self.ml_confidence = round(self.ml_confidence * 0.80, 2)
            elif self.ats_ml_agree:
                if self.spread_confidence:
                    self.spread_confidence = round(min(self.spread_confidence * 1.10, 0.95), 2)
                if self.ml_confidence:
                    self.ml_confidence = round(min(self.ml_confidence * 1.10, 0.95), 2)

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "game_time": self.game_time,
            "models": {
                "ats_margin": self.margin_ats,
                "ou_total": self.ou_total,
                "home_win_prob": self.home_win_prob,
                "ml_edge": round(self.ml_edge, 4),
            },
            "lines": {
                "home_moneyline": self.home_moneyline,
                "away_moneyline": self.away_moneyline,
                "spread": self.spread,
                "over_under": self.over_under,
            },
            "predictions": {
                "home_score": self.predicted_home_score,
                "away_score": self.predicted_away_score,
                "total": self.predicted_total,
                "margin": self.predicted_margin,
            },
            "picks": {
                "moneyline": self.moneyline_pick,
                "ml_confidence": round(self.ml_confidence, 2),
                "spread": self.spread_pick,
                "spread_confidence": round(self.spread_confidence, 2),
                "over_under": self.over_under_pick,
                "ou_confidence": round(self.ou_confidence, 2),
            },
            "conflict": {
                "level": self.conflict_level,
                "details": self.conflict_desc,
                "models_agree": self.ats_ml_agree,
            },
            "reasoning": self.reasoning,
            "team_stats": {"home": self.home_stats, "away": self.away_stats},
        }


class NBAHandicapper:
    """Generates NBA pick cards using XGBoost model predictions."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def handicap_date(self, game_date: str, num_games: int = 10) -> list[NBAPickCard]:
        """Produce pick cards for all games on a given date."""
        from pytz import timezone as _tz
        central = _tz("America/Chicago")
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=central)
        season_year = dt.year
        builder = NBATeamStatsBuilder(self.db)
        all_stats = await builder.build(season_year, num_games=num_games, up_to_date=dt.date())
        if not all_stats:
            return []

        dstart = dt.astimezone(timezone.utc)
        dend = (dt + timedelta(days=1)).astimezone(timezone.utc)
        r = await self.db.execute(
            select(NBAGame).where(NBAGame.date >= dstart, NBAGame.date < dend).order_by(NBAGame.date)
        )
        games = r.scalars().all()
        game_ids = [g.id for g in games]

        # Load lines from betting_lines
        lr = await self.db.execute(_sa_text("""
            SELECT DISTINCT ON (game_id) game_id, spread, over_under,
                   home_moneyline, away_moneyline,
                   home_implied_probability, away_implied_probability
            FROM nba.betting_lines_consolidated
            WHERE game_id = ANY(:gids)
            ORDER BY game_id, recorded_at DESC
        """), {"gids": list(game_ids)})
        lines_by_game: dict[int, dict] = {}
        for row in lr.mappings():
            lines_by_game[row["game_id"]] = dict(row)

        tr = await self.db.execute(select(NBATeam))
        teams = {t.id: t.abbreviation for t in tr.scalars().all()}

        total_pts = sum(s.points_for + s.points_against for s in all_stats.values())
        total_g = sum(s.games for s in all_stats.values()) * 2
        league_avg_total = round(total_pts / max(total_g, 1), 1)

        cards = []
        for game in games:
            home_abbr = teams.get(game.home_team_id, "")
            away_abbr = teams.get(game.away_team_id, "")
            if not home_abbr or not away_abbr:
                continue

            card = NBAPickCard(game, home_abbr, away_abbr, season_avg_total=league_avg_total)
            home_stats = all_stats.get(home_abbr)
            away_stats = all_stats.get(away_abbr)
            card.home_stats = home_stats.to_dict() if home_stats else None
            card.away_stats = away_stats.to_dict() if away_stats else None

            line_row = lines_by_game.get(game.id)
            if line_row:
                card.home_moneyline = line_row.get("home_moneyline")
                card.away_moneyline = line_row.get("away_moneyline")
                card.spread = line_row.get("spread")
                card.over_under = line_row.get("over_under")

            line_simple = type('Line', (), {})()
            line_simple.home_moneyline = card.home_moneyline
            line_simple.away_moneyline = card.away_moneyline
            line_simple.spread = card.spread
            line_simple.over_under = card.over_under

            date_str = game_date
            if isinstance(game.date, datetime):
                date_str = game.date.strftime("%Y-%m-%d")

            margin, _ = await predict_ats(
                game.id, home_abbr, away_abbr, season_year, date_str,
                home_stats, away_stats, line_simple
            )
            card.margin_ats = margin

            ou_total, _ = await predict_ou(
                game.id, home_abbr, away_abbr, season_year, date_str,
                home_stats, away_stats, line_simple
            )
            card.ou_total = ou_total

            ml_prob, _, ml_edge = await predict_ml(
                game.id, home_abbr, away_abbr, season_year, date_str,
                home_stats, away_stats, line_simple
            )
            card.home_win_prob = ml_prob
            card.ml_edge = ml_edge

            try:
                card.predict()
            except RuntimeError:
                continue

            self._generate_reasoning(card, home_stats, away_stats)

            try:
                await self._save_prediction(card)
            except Exception as e:
                logger.warning(f"Could not save prediction for game {card.game_id}: {e}")

            cards.append(card)

        await self.db.commit()
        return cards

    async def _log_error(self, game_id: int, error_type: str, error_message: str):
        """Persist a prediction error to public.prediction_errors."""
        from app.models.public.prediction_error import PredictionError
        try:
            err = PredictionError(
                game_id=game_id,
                sport="nba",
                error_type=error_type,
                error_message=str(error_message)[:500],
            )
            self.db.add(err)
            await self.db.flush()
        except Exception as e:
            logger.warning(f"Failed to log prediction error for game {game_id}: {e}")

    async def handicap_game(self, game_id: int, num_games: int = 10) -> Optional[NBAPickCard]:
        """Generate a pick card for a single game by ID."""
        builder = NBATeamStatsBuilder(self.db)
        all_stats, season_year, game_date, home_abbr, away_abbr = await builder.build_for_game(
            game_id, num_games=num_games
        )
        if not all_stats or not season_year or not game_date:
            await self._log_error(game_id, "no_stats", "build_for_game returned None")
            return None

        date_str = game_date.isoformat() if isinstance(game_date, date) else str(game_date)[:10]

        gr = await self.db.execute(select(NBAGame).where(NBAGame.id == game_id))
        game = gr.scalar_one_or_none()
        if not game:
            await self._log_error(game_id, "no_game", "Game not found in nba.games")
            return None

        lr = await self.db.execute(_sa_text("""
            SELECT spread, over_under, home_moneyline, away_moneyline,
                   home_implied_probability, away_implied_probability
            FROM nba.betting_lines_consolidated
            WHERE game_id = :gid
            ORDER BY recorded_at DESC LIMIT 1
        """), {"gid": game_id})
        game_line = lr.mappings().one_or_none()

        total_pts = sum(s.points_for + s.points_against for s in all_stats.values())
        total_g = sum(s.games for s in all_stats.values())
        season_avg_total = round(total_pts / max(total_g * 2, 1), 1)

        card = NBAPickCard(game, home_abbr, away_abbr, season_avg_total=season_avg_total)
        home_stats = all_stats.get(home_abbr)
        away_stats = all_stats.get(away_abbr)
        card.home_stats = home_stats.to_dict() if home_stats else None
        card.away_stats = away_stats.to_dict() if away_stats else None

        if game_line:
            card.home_moneyline = game_line.get("home_moneyline")
            card.away_moneyline = game_line.get("away_moneyline")
            card.spread = game_line.get("spread")
            card.over_under = game_line.get("over_under")

        line_simple = type('Line', (), {})()
        line_simple.home_moneyline = card.home_moneyline
        line_simple.away_moneyline = card.away_moneyline
        line_simple.spread = card.spread
        line_simple.over_under = card.over_under

        margin, _ = await predict_ats(
            game_id, home_abbr, away_abbr, season_year, date_str,
            home_stats, away_stats, line_simple
        )
        if margin is None:
            await self._log_error(game_id, "model_error", "ATS model returned None")
        card.margin_ats = margin

        ou_total, _ = await predict_ou(
            game_id, home_abbr, away_abbr, season_year, date_str,
            home_stats, away_stats, line_simple
        )
        if ou_total is None:
            await self._log_error(game_id, "model_error", "OU model returned None")
        card.ou_total = ou_total

        ml_prob, _, ml_edge = await predict_ml(
            game_id, home_abbr, away_abbr, season_year, date_str,
            home_stats, away_stats, line_simple
        )
        if ml_prob is None:
            await self._log_error(game_id, "model_error", "ML model returned None")
        card.home_win_prob = ml_prob
        card.ml_edge = ml_edge

        try:
            card.predict()
        except RuntimeError as e:
            await self._log_error(game_id, "model_error", str(e))
            await self.db.commit()
            return None

        self._generate_reasoning(card, home_stats, away_stats)

        try:
            await self._save_prediction(card)
            await self.db.commit()
        except Exception as e:
            logger.warning(f"Could not save prediction for game {game_id}: {e}")
            await self._log_error(game_id, "save_error", str(e))
            await self.db.rollback()

        return card

    def _generate_reasoning(self, card: NBAPickCard, home_stats, away_stats):
        reasons = []
        if home_stats and away_stats:
            hm = home_stats.point_margin
            am = away_stats.point_margin
            if hm > am + 1:
                reasons.append(f"{card.home_team} outscoring opponents by {hm:.2f} PPG vs {card.away_team} at {am:.2f}")
            if home_stats.home_games > 0:
                reasons.append(f"{card.home_team} scoring {home_stats.home_ppg_for:.2f} PPG at home")
            if away_stats.away_games > 0:
                reasons.append(f"{card.away_team} scoring {away_stats.away_ppg_for:.2f} PPG on road")
            reasons.append(f"{card.home_team} {home_stats.recent_form_str} last {len(home_stats.recent_form)}")
            reasons.append(f"{card.away_team} {away_stats.recent_form_str} last {len(away_stats.recent_form)}")
        if card.margin_ats is not None:
            reasons.append(f"ATS margin: {card.home_team} by {card.margin_ats:+.1f}")
        if card.ou_total is not None:
            reasons.append(f"OU total: {card.ou_total:.1f} pts")
        if card.home_win_prob is not None and card.home_moneyline is not None:
            reasons.append(f"Win prob: {card.home_team} {card.home_win_prob:.0%} (market: {card.home_moneyline:+.0f})")
        if card.conflict_level > 0:
            for d in card.conflict_desc:
                reasons.append(f"⚠ Conflict: {d}")
        card.reasoning = reasons

    async def _save_prediction(self, card: NBAPickCard):
        if card.predicted_home_score is None:
            return
        from app.models.nba import NBAGamePrediction
        existing = (await self.db.execute(
            select(NBAGamePrediction).where(
                NBAGamePrediction.game_id == card.game_id,
                NBAGamePrediction.source == "api",
            )
        )).scalar_one_or_none()

        gp = existing or NBAGamePrediction(
            game_id=card.game_id,
            source="api",
        )

        started_r = await self.db.execute(select(NBAGame).where(NBAGame.id == card.game_id))
        started_game = started_r.scalar_one_or_none()
        if started_game and started_game.status and started_game.status.lower() in ('final', 'in_progress'):
            if existing:
                return

        import json as _json
        gp.predicted_home_score = card.predicted_home_score
        gp.predicted_away_score = card.predicted_away_score
        gp.predicted_total = card.predicted_total
        gp.predicted_margin = card.predicted_margin
        gp.margin_conf = card.spread_confidence if card.spread_confidence else None
        gp.rl_conf = card.spread_confidence if card.spread_confidence else None
        gp.ml_conf = card.ml_confidence if card.ml_confidence else None
        gp.ou_conf = card.ou_confidence if card.ou_confidence else None
        gp.ou_pick = card.over_under_pick
        gp.spread_pick = card.spread_pick
        gp.ml_pick = card.moneyline_pick
        gp.ml_odds = card.home_moneyline if card.moneyline_pick == "home" else card.away_moneyline if card.moneyline_pick == "away" else None

        if getattr(card, 'home_stats', None):
            gp.home_stats_json = _json.dumps(card.home_stats)
        if getattr(card, 'away_stats', None):
            gp.away_stats_json = _json.dumps(card.away_stats)

        game_r = await self.db.execute(select(NBAGame).where(NBAGame.id == card.game_id))
        game = game_r.scalar_one_or_none()
        if game and game.home_score is not None:
            hs, aws = int(game.home_score), int(game.away_score)
            if hs + aws > 0 or (started_game and started_game.status and started_game.status.lower() == "final"):
                gp.actual_home_score = hs
                gp.actual_away_score = aws
            gp.actual_total = hs + aws
            gp.actual_margin = hs - aws

            game_is_final = game.status and game.status.lower() == "final"
            if game_is_final:
                predicted_margin = card.predicted_margin or 0
                predicted_total = card.predicted_total or 0
                # Spread result
                if card.spread is not None:
                    am = hs - aws
                    sp = card.spread
                    if abs(am - float(sp)) < 0.3:
                        gp.spread_result = "Push"
                    elif (predicted_margin > float(sp)) == (am > float(sp)):
                        gp.spread_result = "Win"
                    else:
                        gp.spread_result = "Loss"
                # OU result
                if card.over_under is not None:
                    actual_total = hs + aws
                    vegas_ou = card.over_under
                    if abs(actual_total - vegas_ou) < 0.5:
                        gp.ou_result = "Push"
                    elif (predicted_total > vegas_ou) == (actual_total > vegas_ou):
                        gp.ou_result = "Win"
                    else:
                        gp.ou_result = "Loss"
                # ML result
                am = hs - aws
                if am != 0:
                    gp.ml_result = "Win" if (predicted_margin > 0) == (am > 0) else "Loss"

        from sqlalchemy import exc as _sa_exc
        self.db.add(gp)
        try:
            await self.db.flush()
        except _sa_exc.IntegrityError:
            await self.db.rollback()

    async def analyze_matchup(self, home_abbr: str, away_abbr: str,
                               year: int, game_date: str) -> Optional[NBAPickCard]:
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dstart = dt
        dend = dt + timedelta(days=1)
        r = await self.db.execute(
            select(NBAGame).join(NBASeason, NBAGame.season_id == NBASeason.id).where(
                NBASeason.year == year, NBAGame.date >= dstart, NBAGame.date < dend,
            )
        )
        games = r.scalars().all()
        tr = await self.db.execute(select(NBATeam))
        teams = {t.abbreviation: t.id for t in tr.scalars().all()}
        home_id = teams.get(home_abbr.upper())
        away_id = teams.get(away_abbr.upper())
        if not home_id or not away_id:
            return None
        game = None
        for g in games:
            if g.home_team_id == home_id and g.away_team_id == away_id:
                game = g
                break
        if not game:
            return None
        cards = await self.handicap_date(game_date)
        for c in cards:
            if c.game_id == game.id:
                return c
        return None


# ═══════════════════════════════════════════════════════════════════
# Backtest Runner
# ═══════════════════════════════════════════════════════════════════

async def backtest_season(db: AsyncSession, year: int,
                          end_date: Optional[str] = None,
                          num_games: Optional[int] = 10,
                          resume: bool = False) -> dict:
    """
    Run a complete season backtest through all three XGBoost models.

    When resume=True, skips games already in game_predictions (source='api').
    Returns aggregated accuracy stats for all three markets (spread, OU, ML).
    """
    import asyncpg

    # Train all three models on 5-year lookback
    train_years = list(range(year - 5, year))
    logger.info(f"Training models on {train_years} for {year} backtest...")

    ats_model = await train_ats(year, train_years)
    with open("/tmp/nba_ats_temp.pkl", "wb") as f:
        pickle.dump(ats_model, f)
    set_ats_path("/tmp/nba_ats_temp.pkl")
    logger.info("  ATS model trained and loaded")

    ou_model = await train_ou(year, train_years)
    with open("/tmp/nba_ou_temp.pkl", "wb") as f:
        pickle.dump(ou_model, f)
    set_ou_path("/tmp/nba_ou_temp.pkl")
    logger.info("  OU model trained and loaded")

    ml_model = await train_ml(year, train_years)
    with open("/tmp/nba_ml_temp.pkl", "wb") as f:
        pickle.dump(ml_model, f)
    set_ml_path("/tmp/nba_ml_temp.pkl")
    logger.info("  ML model trained and loaded")

    DSN = os.environ.get("DATABASE_URL", "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")
    if "+asyncpg" in DSN:
        DSN = DSN.replace("+asyncpg", "")

    conn = await asyncpg.connect(DSN)
    try:
        season_row = await conn.fetchrow("SELECT id FROM nba.seasons WHERE year=$1", year)
        if not season_row:
            return {"error": f"Season {year} not found"}
        season_id = season_row["id"]

        if not resume:
            await conn.execute("""
                DELETE FROM nba.game_predictions gp
                USING nba.games g
                WHERE gp.game_id = g.id AND g.season_id = $1 AND gp.source = 'api'
            """, season_id)

        games = await conn.fetch("""
            SELECT g.id, g.date::date as game_date, g.home_score, g.away_score,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   bl.home_moneyline, bl.away_moneyline, bl.spread, bl.over_under
            FROM nba.games g
            JOIN nba.teams ht ON ht.id = g.home_team_id
            JOIN nba.teams at ON at.id = g.away_team_id
            LEFT JOIN (
                SELECT DISTINCT ON (game_id) game_id, home_moneyline, away_moneyline, spread, over_under
                FROM nba.betting_lines_consolidated
                ORDER BY game_id, recorded_at DESC
            ) bl ON bl.game_id = g.id
            WHERE g.season_id = $1 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY g.date, g.id
        """, season_id)

        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            games = [g for g in games if g["game_date"] <= end]

        if resume:
            existing_ids = set()
            for row in await conn.fetch(
                "SELECT game_id FROM nba.game_predictions WHERE source='api' "
                "AND game_id = ANY(SELECT id FROM nba.games WHERE season_id=$1)", season_id
            ):
                existing_ids.add(row["game_id"])
            skipped = len([g for g in games if g["id"] in existing_ids])
            games = [g for g in games if g["id"] not in existing_ids]
            logger.info(f"Resume mode: {skipped} already done, {len(games)} remaining")

        logger.info(f"Processing {len(games)} games for {year}...")

        sp_c = 0
        sp_i = 0
        sp_p = 0
        ou_c = 0
        ou_i = 0
        ou_p = 0
        ml_c = 0
        ml_i = 0
        total = len(games)

        for idx, g in enumerate(games):
            home_abbr = g["ha"]
            away_abbr = g["aa"]
            date_str = g["game_date"].isoformat()

            dummy_home = NBATeamStats(home_abbr, 0, year)
            dummy_home.games = 1
            dummy_away = NBATeamStats(away_abbr, 0, year)
            dummy_away.games = 1

            from types import SimpleNamespace
            lo = SimpleNamespace(
                home_moneyline=g["home_moneyline"],
                away_moneyline=g["away_moneyline"],
                spread=g["spread"],
                over_under=g["over_under"],
            )

            try:
                ats_conf = 0.50
                ou_conf = 0.50
                ml_conf = 0.50
                margin, ats_conf_c = await predict_ats(
                    g["id"], home_abbr, away_abbr, year, date_str,
                    dummy_home, dummy_away, lo, conn=conn
                )
                ats_conf = ats_conf_c if ats_conf_c else 0.50
                ou_total, ou_conf_c = await predict_ou(
                    g["id"], home_abbr, away_abbr, year, date_str,
                    dummy_home, dummy_away, lo, conn=conn
                )
                ou_conf = ou_conf_c if ou_conf_c else 0.50
                ml_prob, ml_conf_c, _ = await predict_ml(
                    g["id"], home_abbr, away_abbr, year, date_str,
                    dummy_home, dummy_away, lo, conn=conn
                )
                ml_conf = ml_conf_c if ml_conf_c else 0.50
            except Exception as e:
                logger.warning(f"  Skipping game {g['id']}: {e}")
                continue

            if margin is None or ou_total is None or ml_prob is None:
                continue

            home_score = round((ou_total + margin) / 2, 1)
            away_score = round((ou_total - margin) / 2, 1)
            pred_total = round(home_score + away_score, 1)
            pred_margin = round(margin, 1)
            hs = int(g["home_score"])
            aws = int(g["away_score"])
            am = hs - aws
            atotal = hs + aws

            # Spread result
            sp_res = None
            sp_pick = None
            if lo.spread is not None:
                pred_covers_home = pred_margin > lo.spread
                actual_covers_home = am > lo.spread
                sp_pick = home_abbr if pred_covers_home else away_abbr
                if abs(am - lo.spread) < 0.3:
                    sp_res = "Push"
                elif pred_covers_home == actual_covers_home:
                    sp_res = "Win"
                else:
                    sp_res = "Loss"

            # OU result
            ou_res = None
            if lo.over_under is not None:
                if abs(atotal - lo.over_under) < 0.5:
                    ou_res = "Push"
                elif (pred_total > lo.over_under) == (atotal > lo.over_under):
                    ou_res = "Win"
                else:
                    ou_res = "Loss"

            # ML result
            ml_res = None
            if am != 0:
                ml_res = "Win" if (pred_margin > 0) == (am > 0) else "Loss"

            def _pl(res, odds):
                if not odds:
                    return 0.0
                if res == "Win":
                    return round(100 * (100.0 / abs(odds) if odds < 0 else odds / 100.0), 2)
                if res == "Loss":
                    return -100.0
                return 0.0

            ml_odds = lo.home_moneyline if pred_margin > 0 else lo.away_moneyline

            await conn.execute("""
                INSERT INTO nba.game_predictions (
                    game_id, source,
                    predicted_home_score, predicted_away_score, predicted_total, predicted_margin,
                    rl_conf, ou_conf, ml_conf, margin_conf,
                    spread_pick, ou_pick, ml_pick,
                    actual_home_score, actual_away_score, actual_total, actual_margin,
                    spread_result, ou_result, ml_result,
                    ats_odds, ou_odds, ml_odds, ats_profit, ou_profit, ml_profit
                ) VALUES ($1,'api',
                    $2,$3,$4,$5,
                    $20,$21,$22,$23,
                    $6,$7,$8,
                    $9,$10,$11,$12,
                    $13,$14,$15,
                    -110,-110,$16,$17,$18,$19
                ) ON CONFLICT (game_id, source) DO NOTHING
            """,
                g["id"], home_score, away_score, pred_total, pred_margin,
                sp_pick,
                "Over" if lo.over_under is not None and pred_total > lo.over_under else (
                    "Under" if lo.over_under is not None else None),
                "home" if pred_margin > 0 else "away",
                hs, aws, atotal, am,
                sp_res, ou_res, ml_res,
                ml_odds, _pl(sp_res, -110), _pl(ou_res, -110),
                _pl(ml_res, ml_odds) if ml_res else None,
                ats_conf, ou_conf, ml_conf, ats_conf
            )

            if sp_res == "Win":
                sp_c += 1
            elif sp_res == "Loss":
                sp_i += 1
            elif sp_res == "Push":
                sp_p += 1
            if ou_res == "Win":
                ou_c += 1
            elif ou_res == "Loss":
                ou_i += 1
            elif ou_res == "Push":
                ou_p += 1
            if ml_res == "Win":
                ml_c += 1
            elif ml_res == "Loss":
                ml_i += 1

            if (idx + 1) % 50 == 0:
                logger.info(f"  [{idx+1}/{total}] saved {sp_c+sp_i+sp_p} games")

        sp_t = sp_c + sp_i
        ou_t = ou_c + ou_i
        ml_t = ml_c + ml_i

        db_results = {
            "season": year,
            "total_games": sp_t,
            "spread": {"correct": sp_c, "incorrect": sp_i, "pushes": sp_p,
                        "pct": round(sp_c / max(sp_t, 1), 3)},
            "over_under": {"correct": ou_c, "incorrect": ou_i, "pushes": ou_p,
                            "pct": round(ou_c / max(ou_t, 1), 3)},
            "moneyline": {"correct": ml_c, "incorrect": ml_i,
                           "pct": round(ml_c / max(ml_t, 1), 3)},
        }

        logger.info(f"\nBacktest {year} complete!")
        logger.info(f"  Spread: {sp_c}/{sp_t} = {db_results['spread']['pct']*100:.1f}%")
        logger.info(f"  OU:     {ou_c}/{ou_t} = {db_results['over_under']['pct']*100:.1f}%")
        logger.info(f"  ML:     {ml_c}/{ml_t} = {db_results['moneyline']['pct']*100:.1f}%")

        return db_results
    finally:
        await conn.close()
