"""
MLB handicapping engine — team stats, daily pick cards, matchup analysis.

Generates structured pick cards using XGBoost model predictions.
Raises RuntimeError if no trained model is available — no heuristic fallback.

Architecture mirrors NFL engine.py (MatchupAnalysis → Handicapper pattern):
  - MLBHandicapper loads 3 models (ATS/OU/ML) and predicts margin, total, win prob
  - MLBPickCard combines them with conflict detection → final score → picks
  - backtest_season() runs historical evaluation
"""
import logging
import os
import pickle
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from typing import Optional

# import numpy as np (no longer used at module level)

from sqlalchemy import select, text as _sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import MLBGames, MLBTeam, MLBSeason

logger = logging.getLogger("earl.mlb_handicapping")

logger = logging.getLogger("earl.mlb_handicapping")
from app.handicapping.mlb.mlb_xgb_model_ats import predict_ats, set_model_path as set_ats_path, train_model as train_ats
from app.handicapping.mlb.mlb_xgb_model_ou import predict_ou, set_model_path as set_ou_path, train_model as train_ou
from app.handicapping.calibrate_confidence import calibrate
CURRENT_YEAR = 2026


# ═══════════════════════════════════════════════════════════════════
# MLB Team Stats
# ═══════════════════════════════════════════════════════════════════

class MLBTeamStats:
    """Per-team aggregated offensive and defensive stats."""

    def __init__(self, team_abbr: str, team_id: int, year: int):
        self.team_abbr = team_abbr
        self.team_id = team_id
        self.year = year
        self.games = 0
        self.home_games = 0
        self.away_games = 0
        self.runs_for = 0.0
        self.runs_against = 0.0
        self.ats_wins = 0
        self.ats_losses = 0
        self.over_hits = 0
        self.under_hits = 0
        self.ml_wins = 0
        self.ml_losses = 0
        self.home_runs_for = 0.0
        self.home_runs_against = 0.0
        self.away_runs_for = 0.0
        self.away_runs_against = 0.0
        self.recent_form = []
        self.last_10_wins = 0
        self.last_10_losses = 0

    @property
    def rpg_for(self) -> float:
        return round(self.runs_for / max(self.games, 1), 2)

    @property
    def rpg_against(self) -> float:
        return round(self.runs_against / max(self.games, 1), 2)

    @property
    def run_margin(self) -> float:
        return round(self.rpg_for - self.rpg_against, 2)

    @property
    def home_rpg_for(self) -> float:
        return round(self.home_runs_for / max(self.home_games, 1), 2)

    @property
    def home_rpg_against(self) -> float:
        return round(self.home_runs_against / max(self.home_games, 1), 2)

    @property
    def away_rpg_for(self) -> float:
        return round(self.away_runs_for / max(self.away_games, 1), 2)

    @property
    def away_rpg_against(self) -> float:
        return round(self.away_runs_against / max(self.away_games, 1), 2)

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
            "rpg_for": self.rpg_for, "rpg_against": self.rpg_against,
            "run_margin": self.run_margin,
            "home_rpg_for": self.home_rpg_for, "home_rpg_against": self.home_rpg_against,
            "away_rpg_for": self.away_rpg_for, "away_rpg_against": self.away_rpg_against,
            "win_pct": self.win_pct, "ats_pct": self.ats_pct, "over_pct": self.over_pct,
            "recent_form": self.recent_form_str,
            "last_10_wins": self.last_10_wins, "last_10_losses": self.last_10_losses,
        }


class MLBTeamStatsBuilder:
    """Builds team strength metrics from game scores."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        year: int,
        num_games: Optional[int] = None,
        up_to_date: Optional[date] = None,
    ) -> dict[str, MLBTeamStats]:
        season_r = await self.db.execute(
            select(MLBSeason).where(MLBSeason.year == year)
        )
        season = season_r.scalar_one_or_none()
        if not season:
            return {}

        conditions = [
            MLBGames.season_id == season.id,
            MLBGames.home_score.isnot(None),
            MLBGames.away_score.isnot(None),
        ]
        if up_to_date:
            conditions.append(MLBGames.date < datetime.combine(up_to_date, datetime.min.time(), tzinfo=timezone.utc))

        r = await self.db.execute(
            select(MLBGames).where(*conditions).order_by(MLBGames.date)
        )
        all_games = r.scalars().all()
        if not all_games:
            return {}

        tr = await self.db.execute(select(MLBTeam))
        teams = {t.id: t.abbreviation for t in tr.scalars().all()}

        all_stats: dict[str, MLBTeamStats] = {}
        for tid, tabbr in teams.items():
            all_stats[tabbr] = MLBTeamStats(tabbr, tid, year)

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
            home.games += 1; home.home_games += 1
            home.runs_for += home_score; home.runs_against += away_score
            home.home_runs_for += home_score; home.home_runs_against += away_score
            away.games += 1; away.away_games += 1
            away.runs_for += away_score; away.runs_against += home_score
            away.away_runs_for += away_score; away.away_runs_against += home_score
            if home_score > away_score:
                home.ml_wins += 1; away.ml_losses += 1
                home.recent_form.append("W"); away.recent_form.append("L")
            else:
                home.ml_losses += 1; away.ml_wins += 1
                home.recent_form.append("L"); away.recent_form.append("W")

        for abbr, stats in all_stats.items():
            last10 = [r for r in stats.recent_form[-10:]]
            stats.last_10_wins = last10.count("W")
            stats.last_10_losses = last10.count("L")

        return all_stats

    async def build_for_game(
        self,
        game_id: int,
        num_games: int = 10,
    ) -> tuple[Optional[dict[str, MLBTeamStats]], Optional[int], Optional[date], Optional[str], Optional[str]]:
        """Build team stats for just the two teams in a specific game.

        Returns (stats_dict, season_year, game_date, home_abbr, away_abbr)
        or (None, None, None, None, None) if game not found.

        This is orders of magnitude cheaper than build() which does all 30 teams.
        """
        gr = await self.db.execute(select(MLBGames).where(MLBGames.id == game_id))
        game = gr.scalar_one_or_none()
        if not game or not game.home_team_id or not game.away_team_id or not game.date:
            return None, None, None, None, None

        sr = await self.db.execute(select(MLBSeason).where(MLBSeason.id == game.season_id))
        season = sr.scalar_one_or_none()
        if not season:
            return None, None, None, None, None
        season_year = season.year

        tr = await self.db.execute(select(MLBTeam).where(
            MLBTeam.id.in_([game.home_team_id, game.away_team_id])
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
            MLBGames.season_id == game.season_id,
            MLBGames.home_score.isnot(None),
            MLBGames.away_score.isnot(None),
            MLBGames.date < cutoff,
            ((MLBGames.home_team_id == game.home_team_id) |
             (MLBGames.away_team_id == game.home_team_id) |
             (MLBGames.home_team_id == game.away_team_id) |
             (MLBGames.away_team_id == game.away_team_id)),
        ]
        r = await self.db.execute(
            select(MLBGames).where(*conditions).order_by(MLBGames.date.desc())
        )
        past_games = r.scalars().all()

        all_stats: dict[str, MLBTeamStats] = {}
        for abbr in [home_abbr, away_abbr]:
            tid = game.home_team_id if abbr == home_abbr else game.away_team_id
            all_stats[abbr] = MLBTeamStats(abbr, tid, season_year)

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

            home.runs_for += hscore
            home.runs_against += ascore
            away.runs_for += ascore
            away.runs_against += hscore

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
# MLB Matchup Analysis (Pick Card)
# ═══════════════════════════════════════════════════════════════════

class MLBPickCard:
    """Handicapping analysis for a single MLB game, driven by XGBoost models.

    Mirrors NFL MatchupAnalysis: combines ATS margin + OU total + ML win prob
    into a predicted score, then generates picks with conflict-adjusted confidence.
    """

    def __init__(self, game: MLBGames, home_abbr: str, away_abbr: str,
                 season_avg_runs: float = 4.5):
        self.game_id = game.id
        self.home_team = home_abbr
        self.away_team = away_abbr
        self.game_time = game.date.isoformat() if game.date else None
        self.season_avg_runs = season_avg_runs

        # Market lines
        self.home_moneyline: Optional[int] = None
        self.away_moneyline: Optional[int] = None
        self.run_line: Optional[float] = None
        self.over_under: Optional[float] = None

        # Three model predictions (set by MLBHandicapper before predict())
        self.margin_ats: Optional[float] = None     # from ATS model
        self.ou_total: Optional[float] = None        # from OU model
        self.home_win_prob: Optional[float] = None   # from ML model
        self.ml_edge: float = 0.0                    # edge over market implied

        # Derived final score (from margin + total)
        self.predicted_home_runs: Optional[float] = None
        self.predicted_away_runs: Optional[float] = None
        self.predicted_total: Optional[float] = None
        self.predicted_margin: Optional[float] = None

        # Picks
        self.moneyline_pick: Optional[str] = None
        self.ml_confidence: float = 0.0
        self.run_line_pick: Optional[str] = None
        self.rl_confidence: float = 0.0
        self.over_under_pick: Optional[str] = None
        self.ou_confidence: float = 0.0

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
        total = self.ou_total
        margin = self.margin_ats

        # ── 3. Predicted score ──
        self.predicted_home_runs = round((total + margin) / 2, 2)
        self.predicted_away_runs = round((total - margin) / 2, 2)
        self.predicted_total = round(self.predicted_home_runs + self.predicted_away_runs, 2)
        self.predicted_margin = round(margin, 2)

        # ── 4. Run line pick — spread convention: negative = home favored, positive = home underdog
        # margin + spread > 0  → home covers. margin + spread < 0 → away covers.
        if self.margin_ats is not None and self.run_line is not None:
            eff = margin + self.run_line
            if eff > 0:
                self.run_line_pick = self.home_team
                self.rl_confidence = round(min(0.5 + abs(eff) * 0.4, 0.90), 2)
            else:
                self.run_line_pick = self.away_team
                self.rl_confidence = round(min(0.5 + abs(eff) * 0.4, 0.90), 2)

        # ── 5. Moneyline pick (from predicted margin) ──
        # ML confidence is derived purely from ATS margin certainty:
        # larger predicted margin = higher confidence
        if margin > 0:
            self.moneyline_pick = "home"
        elif margin < 0:
            self.moneyline_pick = "away"

        if self.moneyline_pick:
            # ML confidence from margin magnitude (simpler, no ML model needed)
            self.ml_confidence = round(min(0.50 + abs(margin) * 0.12, 0.95), 2)
            # ml_edge: how far over 50% we are — reflects margin certainty
            self.ml_edge = self.ml_confidence - 0.50

        # ── 6. Over/Under pick (from OU model) ──
        if self.ou_total is not None and self.over_under is not None:
            diff = self.ou_total - self.over_under
            if diff > 0:
                self.over_under_pick = "over"
                self.ou_confidence = round(min(0.5 + abs(diff) * 0.5, 0.90), 2)
            else:
                self.over_under_pick = "under"
                self.ou_confidence = round(min(0.5 + abs(diff) * 0.5, 0.90), 2)

        # ── 7. No conflict detection — ML confidence is derived from ATS margin only

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
                "run_line": self.run_line,
                "over_under": self.over_under,
            },
            "predictions": {
                "home_runs": self.predicted_home_runs,
                "away_runs": self.predicted_away_runs,
                "total": self.predicted_total,
                "margin": self.predicted_margin,
            },
            "picks": {
                "moneyline": self.moneyline_pick,
                "ml_confidence": round(self.ml_confidence, 2),
                "run_line": self.run_line_pick,
                "rl_confidence": round(self.rl_confidence, 2),
                "over_under": self.over_under_pick,
                "ou_confidence": round(self.ou_confidence, 2),
            },

            "reasoning": self.reasoning,
            "team_stats": {"home": self.home_stats, "away": self.away_stats},
        }


class MLBHandicapper:
    """Generates MLB pick cards using XGBoost model predictions.

    Loads ATS, OU, and ML models on first use. Each model is used for
    its specialty: ATS → margin, OU → total, ML → win probability.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def handicap_date(self, game_date: str, num_games: int = 10) -> list[MLBPickCard]:
        """Produce pick cards for all games on a given date.

        1. Build team stats from games before the date
        2. Load betting lines
        3. For each game: run ATS, OU, and ML models
        4. Combine into predicted score + picks with conflict detection
        5. Save to game_predictions table
        """
        from pytz import timezone as _tz
        central = _tz("America/Chicago")
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=central)
        season_year = dt.year
        builder = MLBTeamStatsBuilder(self.db)
        all_stats = await builder.build(season_year, num_games=num_games, up_to_date=dt.date())
        if not all_stats:
            return []

        dstart = dt.astimezone(timezone.utc)
        dend = (dt + timedelta(days=1)).astimezone(timezone.utc)
        r = await self.db.execute(
            select(MLBGames).where(MLBGames.date >= dstart, MLBGames.date < dend).order_by(MLBGames.date)
        )
        games = r.scalars().all()
        game_ids = [g.id for g in games]
        # Use betting_lines_consolidated (single source of truth) instead of raw betting_lines
        from sqlalchemy import text as sa_text
        lr = await self.db.execute(sa_text("""
            SELECT game_id, spread, over_under, home_moneyline, away_moneyline,
                   home_implied_probability, away_implied_probability,
                   opening_spread, opening_total,
                   opening_home_moneyline, opening_away_moneyline
            FROM mlb.betting_lines_consolidated
            WHERE game_id = ANY(:gids)
        """), {"gids": list(game_ids)})
        lines_by_game: dict[int, dict] = {}
        for row in lr.mappings():
            lines_by_game[row["game_id"]] = dict(row)
        tr = await self.db.execute(select(MLBTeam))
        teams = {t.id: t.abbreviation for t in tr.scalars().all()}
        total_pts = sum(s.runs_for + s.runs_against for s in all_stats.values())
        total_g = sum(s.games for s in all_stats.values()) * 2
        league_avg_runs = round(total_pts / max(total_g, 1), 1)

        cards = []
        for game in games:
            home_abbr = teams.get(game.home_team_id, "")
            away_abbr = teams.get(game.away_team_id, "")
            if not home_abbr or not away_abbr:
                continue

            card = MLBPickCard(game, home_abbr, away_abbr, season_avg_runs=league_avg_runs)
            home_stats = all_stats.get(home_abbr)
            away_stats = all_stats.get(away_abbr)
            card.home_stats = home_stats.to_dict() if home_stats else None
            card.away_stats = away_stats.to_dict() if away_stats else None

            line_row = lines_by_game.get(game.id)
            if line_row:
                card.home_moneyline = line_row.get("home_moneyline")
                card.away_moneyline = line_row.get("away_moneyline")
                card.run_line = line_row.get("spread")
                card.over_under = line_row.get("over_under")

            # ── Run all three models (like NFL engine.py does) ──
            line_simple = type('Line', (), {})()
            line_simple.home_moneyline = card.home_moneyline
            line_simple.away_moneyline = card.away_moneyline
            line_simple.spread = card.run_line
            line_simple.over_under = card.over_under

            # All three models must succeed — no fallback predictions
            margin, _ = await predict_ats(
                game.id, home_abbr, away_abbr, season_year, game_date,
                home_stats, away_stats, line_simple
            )
            card.margin_ats = margin

            ou_total, _ = await predict_ou(
                game.id, home_abbr, away_abbr, season_year, game_date,
                home_stats, away_stats, line_simple
            )
            card.ou_total = ou_total

            # ML model not used — confidence derived from ATS margin
            card.home_win_prob = None

            # ── Combine into score + picks (raises if any model is None) ──
            card.predict()
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
                sport="mlb",
                error_type=error_type,
                error_message=str(error_message)[:500],
            )
            self.db.add(err)
            await self.db.flush()
        except Exception as e:
            logger.warning(f"Failed to log prediction error for game {game_id}: {e}")

    async def handicap_game(self, game_id: int, num_games: int = 10) -> Optional[MLBPickCard]:
        """Generate a pick card for a single game by ID.

        Uses build_for_game() to fetch stats for just the two teams involved
        instead of all 30. Designed to be called by odds-refresh tasks when
        a single game's lines change.

        All failures are logged to public.prediction_errors for diagnostics.
        """
        builder = MLBTeamStatsBuilder(self.db)
        all_stats, season_year, game_date, home_abbr, away_abbr = await builder.build_for_game(
            game_id, num_games=num_games
        )
        if not all_stats or not season_year or not game_date:
            await self._log_error(game_id, "no_stats", "build_for_game returned None — no team stats available for this game date")
            return None

        # Reconstruct date string for the prediction functions
        date_str = game_date.isoformat() if isinstance(game_date, date) else str(game_date)[:10]

        # Fetch the game + lines
        gr = await self.db.execute(select(MLBGames).where(MLBGames.id == game_id))
        game = gr.scalar_one_or_none()
        if not game:
            await self._log_error(game_id, "no_game", "Game not found in mlb.games")
            return None

        from sqlalchemy import text as sa_text
        lr = await self.db.execute(sa_text("""
            SELECT spread, over_under, home_moneyline, away_moneyline,
                   home_implied_probability, away_implied_probability
            FROM mlb.betting_lines_consolidated
            WHERE game_id = :gid
        """), {"gid": game_id})
        game_line = lr.mappings().one_or_none()

        # Season avg as fallback for OU
        total_runs = sum(s.runs_for + s.runs_against for s in all_stats.values())
        total_g = sum(s.games for s in all_stats.values())
        season_avg_runs = round(total_runs / max(total_g * 2, 1), 1)

        card = MLBPickCard(game, home_abbr, away_abbr, season_avg_runs=season_avg_runs)
        home_stats = all_stats.get(home_abbr)
        away_stats = all_stats.get(away_abbr)
        card.home_stats = home_stats.to_dict() if home_stats else None
        card.away_stats = away_stats.to_dict() if away_stats else None

        if game_line:
            card.home_moneyline = game_line.get("home_moneyline")
            card.away_moneyline = game_line.get("away_moneyline")
            card.run_line = game_line.get("spread")
            card.over_under = game_line.get("over_under")

        line_simple = type('Line', (), {})()
        line_simple.home_moneyline = card.home_moneyline
        line_simple.away_moneyline = card.away_moneyline
        line_simple.spread = card.run_line
        line_simple.over_under = card.over_under

        margin, _ = await predict_ats(
            game_id, home_abbr, away_abbr, season_year, date_str,
            home_stats, away_stats, line_simple
        )
        if margin is None:
            await self._log_error(game_id, "model_error", "ATS model returned None — model file missing or DB connection failed")
        card.margin_ats = margin

        ou_total, _ = await predict_ou(
            game_id, home_abbr, away_abbr, season_year, date_str,
            home_stats, away_stats, line_simple
        )
        if ou_total is None:
            await self._log_error(game_id, "model_error", "OU model returned None — model file missing or DB connection failed")
        card.ou_total = ou_total

        # ML model not used — confidence derived from ATS margin
        card.home_win_prob = None

        card.predict()

        # Calibrate confidence and compute EV (same as backtest season)
        from app.handicapping.calibrate_confidence import calibrate as _cal
        def _pp(o):
            return (100.0 * 100.0 / float(abs(o))) if o < 0 else float(o) if o else 0.0
        raw_ats = card.rl_confidence if hasattr(card, 'rl_confidence') and card.rl_confidence else 0.50
        raw_ou = card.ou_confidence if hasattr(card, 'ou_confidence') and card.ou_confidence else 0.50
        raw_ml = card.ml_confidence if hasattr(card, 'ml_confidence') and card.ml_confidence else 0.50
        cal_ats = _cal(max(min(raw_ats, 0.99), 0.01), "rl", sport="mlb")
        cal_ou = _cal(max(min(raw_ou, 0.99), 0.01), "ou", sport="mlb")
        cal_ml = _cal(max(min(raw_ml, 0.99), 0.01), "ml", sport="mlb")
        card.ats_ev = round((cal_ats * _pp(card.row_line_odds or -110)) - ((1 - cal_ats) * 100), 2)
        card.ou_ev = round((cal_ou * _pp(-110)) - ((1 - cal_ou) * 100), 2)
        card.ml_ev = round((cal_ml * _pp(card.money_line_odds or 100)) - ((1 - cal_ml) * 100), 2) if card.money_line_odds else None

        self._generate_reasoning(card, home_stats, away_stats)

        try:
            await self._save_prediction(card)
            await self.db.commit()
        except Exception as e:
            logger.warning(f"Could not save prediction for game {game_id}: {e}")
            await self._log_error(game_id, "save_error", str(e))
            await self.db.rollback()

        return card

    def _generate_reasoning(self, card: MLBPickCard, home_stats, away_stats):
        reasons = []
        if home_stats and away_stats:
            hm = home_stats.run_margin
            am = away_stats.run_margin
            if hm > am + 0.5:
                reasons.append(f"{card.home_team} outscoring opponents by {hm:.2f} RPG vs {card.away_team} at {am:.2f}")
            if home_stats.home_games > 0:
                reasons.append(f"{card.home_team} scoring {home_stats.home_rpg_for:.2f} RPG at home")
            if away_stats.away_games > 0:
                reasons.append(f"{card.away_team} scoring {away_stats.away_rpg_for:.2f} RPG on road")
            reasons.append(f"{card.home_team} {home_stats.recent_form_str} last {len(home_stats.recent_form)}")
            reasons.append(f"{card.away_team} {away_stats.recent_form_str} last {len(away_stats.recent_form)}")
            if home_stats.over_pct is not None and home_stats.over_pct > 0.55:
                reasons.append(f"{card.home_team} hitting over {home_stats.over_pct:.0%} of games")
            if away_stats.over_pct is not None and away_stats.over_pct < 0.45:
                reasons.append(f"{card.away_team} hitting under {1-away_stats.over_pct:.0%} of games")
        if card.margin_ats is not None:
            reasons.append(f"ATS margin: {card.home_team} by {card.margin_ats:+.2f} runs")
        if card.ou_total is not None:
            reasons.append(f"OU total: {card.ou_total:.1f} runs")
        if card.margin_ats is not None and card.home_moneyline is not None:
            reasons.append(f"Predicted margin gives {card.home_team} {min(0.5+abs(card.margin_ats)*0.12,0.95):.0%} win confidence")
        card.reasoning = reasons

    async def _save_prediction(self, card: MLBPickCard):
        if card.predicted_home_runs is None:
            return
        from app.models.mlb import MLBGamePrediction
        existing = (await self.db.execute(
            select(MLBGamePrediction).where(
                MLBGamePrediction.game_id == card.game_id,
                MLBGamePrediction.source == "api",
            )
        )).scalar_one_or_none()

        gp = existing or MLBGamePrediction(
            game_id=card.game_id,
            source="api",
        )

        # Don't update if game has already started (has a posted score)
        # Still allow save if no prediction exists yet (missed by backtest)
        started_r = await self.db.execute(select(MLBGames).where(MLBGames.id == card.game_id))
        started_game = started_r.scalar_one_or_none()
        if started_game and started_game.status and started_game.status.lower() in ('final', 'in_progress'):
            if existing:
                return

        gp.predicted_home_runs = card.predicted_home_runs
        gp.predicted_away_runs = card.predicted_away_runs
        gp.predicted_total = card.predicted_total
        gp.predicted_margin = card.predicted_margin
        gp.margin_conf = card.rl_confidence if card.rl_confidence else None
        gp.rl_conf = card.rl_confidence if card.rl_confidence else None
        gp.ml_conf = card.ml_confidence if card.ml_confidence else None
        gp.ou_conf = card.ou_confidence if card.ou_confidence else None
        gp.ou_pick = card.over_under_pick
        gp.run_line_pick = card.run_line_pick
        gp.ml_pick = card.moneyline_pick
        gp.ml_odds = card.home_moneyline if card.moneyline_pick == "home" else card.away_moneyline if card.moneyline_pick == "away" else None
        # Save enriched metadata (team stats, situational, splits)
        import json as _json
        if getattr(card, 'home_stats', None):
            gp.home_stats_json = _json.dumps(card.home_stats)
        if getattr(card, 'away_stats', None):
            gp.away_stats_json = _json.dumps(card.away_stats)
        game_r = await self.db.execute(select(MLBGames).where(MLBGames.id == card.game_id))
        game = game_r.scalar_one_or_none()
        if game and game.home_score is not None:
            hs, aws = int(game.home_score), int(game.away_score)
            # Only save actual scores if the game has meaningful scoring data
            # (0-0 is almost certainly an in-progress state, not a real final)
            if hs + aws > 0 or (started_game and started_game.status and started_game.status.lower() == "final"):
                gp.actual_home_runs = hs
                gp.actual_away_runs = aws
            gp.actual_total = hs + aws
            gp.actual_margin = hs - aws

            # Only compute win/loss/push results for games that have actually finished
            game_is_final = game.status and game.status.lower() == "final"
            if game_is_final:
                predicted_margin = card.predicted_margin or 0
                predicted_total = card.predicted_total or 0
                if card.run_line is not None:
                    am = hs - aws
                    sp = card.run_line
                    if abs(am - float(sp)) < 0.3:
                        gp.run_line_result = "Push"
                    elif (predicted_margin + float(sp) > 0) == (am + float(sp) > 0):
                        gp.run_line_result = "Win"
                    else:
                        gp.run_line_result = "Loss"
                if card.over_under is not None:
                    actual_total = hs + aws
                    vegas_ou = card.over_under
                    if abs(actual_total - vegas_ou) < 0.5:
                        gp.ou_result = "Push"
                    elif (predicted_total > vegas_ou) == (actual_total > vegas_ou):
                        gp.ou_result = "Win"
                    else:
                        gp.ou_result = "Loss"
                am = hs - aws
                if am != 0:
                    gp.ml_result = "Win" if (predicted_margin > 0) == (am > 0) else "Loss"
        from sqlalchemy import exc as _sa_exc
        self.db.add(gp)
        try:
            await self.db.flush()
        except _sa_exc.IntegrityError:
            await self.db.rollback()

    async def analyze_matchup(self, home_abbr: str, away_abbr: str, year: int, game_date: str) -> Optional[MLBPickCard]:
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dstart = dt
        dend = dt + timedelta(days=1)
        r = await self.db.execute(
            select(MLBGames).join(MLBSeason, MLBGames.season_id == MLBSeason.id).where(
                MLBSeason.year == year, MLBGames.date >= dstart, MLBGames.date < dend,
            )
        )
        games = r.scalars().all()
        tr = await self.db.execute(select(MLBTeam))
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

    Matches the NFL pattern: iterates all games, runs all three models,
    evaluates picks, saves to game_predictions. Uses dummy stats because
    each predict function queries its own features from the DB.

    When resume=True, skips games already in game_predictions (source='api')
    so you can pick up where you left off.

    Returns aggregated accuracy stats for all three markets (RL, OU, ML).
    """
    import asyncpg

    # ── Load pre-trained models (trained by standalone backtest) ──
    # These are saved to /app/data/mlb_{ats,ou}_{year}.pkl by train_model()
    # in the model files, and should match the standalone backtest exactly.
    ats_path = f"/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ats_{year}.pkl"
    ou_path = f"/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ou_{year}.pkl"
    if not os.path.exists(ats_path):
        logger.warning(f"ATS model not found at {ats_path} -- falling back to training")
        train_years = list(range(2021, year))
        ats_model = await train_ats(year, train_years)
        with open(ats_path, "wb") as f:
            pickle.dump(ats_model, f)
    if not os.path.exists(ou_path):
        logger.warning(f"OU model not found at {ou_path} -- falling back to training")
        train_years = list(range(2021, year))
        ou_model = await train_ou(year, train_years)
        with open(ou_path, "wb") as f:
            pickle.dump(ou_model, f)
    set_ats_path(ats_path)
    set_ou_path(ou_path)
    logger.info(f"  ATS model: {ats_path}")
    logger.info(f"  OU model: {ou_path}")

    DSN = os.environ.get("DATABASE_URL", "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")
    if "+asyncpg" in DSN:
        DSN = DSN.replace("+asyncpg", "")

    conn = await asyncpg.connect(DSN)
    try:
        season_row = await conn.fetchrow("SELECT id FROM mlb.seasons WHERE year=$1", year)
        if not season_row:
            return {"error": f"Season {year} not found"}
        season_id = season_row["id"]
        
        # Clear existing api predictions (skip when resuming)
        if not resume:
            await conn.execute("""
                DELETE FROM mlb.game_predictions gp
                USING mlb.games g
                WHERE gp.game_id = g.id AND g.season_id = $1 AND gp.source = 'api'
            """, season_id)

        # Get all completed games with lines
        games = await conn.fetch("""
            SELECT g.id, g.date::date as game_date, g.home_score, g.away_score,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   bl.home_moneyline, bl.away_moneyline, bl.spread, bl.over_under,
                   bl.spread_home_odds, bl.spread_away_odds
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            LEFT JOIN mlb.betting_lines_consolidated bl ON bl.game_id = g.id
            WHERE g.season_id = $1 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY g.date, g.id
        """, season_id)

        # Apply end_date filter
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            games = [g for g in games if g["game_date"] <= end]

        # When resuming, skip games already in game_predictions
        if resume:
            existing_ids = set()
            for row in await conn.fetch(
                "SELECT game_id FROM mlb.game_predictions WHERE source='api' "
                "AND game_id = ANY(SELECT id FROM mlb.games WHERE season_id=$1)", season_id
            ):
                existing_ids.add(row["game_id"])
            skipped = len([g for g in games if g["id"] in existing_ids])
            games = [g for g in games if g["id"] not in existing_ids]
            logger.info(f"Resume mode: {skipped} already done, {len(games)} remaining")

        logger.info(f"Processing {len(games)} games for {year}...")

        rl_c = 0; rl_i = 0; rl_p = 0
        ou_c = 0; ou_i = 0; ou_p = 0
        ml_c = 0; ml_i = 0
        total = len(games)

        for idx, g in enumerate(games):
            home_abbr = g["ha"]; away_abbr = g["aa"]
            date_str = g["game_date"].isoformat()

            # Dummy stats — predict functions query their own features via asyncpg
            dummy_home = MLBTeamStats(home_abbr, 0, year)
            dummy_home.games = 1; dummy_away = MLBTeamStats(away_abbr, 0, year)
            dummy_away.games = 1

            # SimpleNamespace for line object
            from types import SimpleNamespace
            lo = SimpleNamespace(
                home_moneyline=g["home_moneyline"],
                away_moneyline=g["away_moneyline"],
                spread=g["spread"],
                over_under=g["over_under"],
                spread_home_odds=g["spread_home_odds"],
                spread_away_odds=g["spread_away_odds"],
            )

            try:
                margin, ats_conf = await predict_ats(g["id"], home_abbr, away_abbr, year, date_str,
                                                dummy_home, dummy_away, lo, conn=conn)
                ats_conf = ats_conf if ats_conf else 0.50
                ou_total, ou_conf = await predict_ou(g["id"], home_abbr, away_abbr, year, date_str,
                                                dummy_home, dummy_away, lo, conn=conn)
                ou_conf = ou_conf if ou_conf else 0.50
                # ML confidence from ATS margin
                ml_conf = round(min(0.50 + abs(margin) * 0.12, 0.95), 2) if margin else 0.50
            except Exception as e:
                logger.warning(f"  Skipping game {g['id']}: {e}")
                continue

            if margin is None or ou_total is None:
                continue

            home_runs = round((ou_total + margin) / 2, 1)
            away_runs = round((ou_total - margin) / 2, 1)
            pred_total = round(home_runs + away_runs, 1)
            pred_margin = round(margin, 1)
            hs = int(g["home_score"]); aws = int(g["away_score"])
            am = hs - aws; atotal = hs + aws

            # RL result — spread follows convention: negative = home favored
            rl_res = None
            rl_pick = None
            if lo.spread is not None:
                pred_covers_home = pred_margin + lo.spread > 0
                actual_covers_home = am + lo.spread > 0
                rl_pick = home_abbr if pred_covers_home else away_abbr
                if abs(am + lo.spread) < 0.3:
                    rl_res = "Push"
                elif pred_covers_home == actual_covers_home:
                    rl_res = "Win"
                else:
                    rl_res = "Loss"

            # OU result
            ou_res = None
            if lo.over_under is not None:
                if abs(atotal - lo.over_under) < 0.5: ou_res = "Push"
                elif (pred_total > lo.over_under) == (atotal > lo.over_under): ou_res = "Win"
                else: ou_res = "Loss"

            # ML result
            ml_res = None
            if am != 0:
                ml_res = "Win" if (pred_margin > 0) == (am > 0) else "Loss"

            def _pl(res, odds):
                if not odds: return 0.0
                if res == "Win": return round(100 * (100.0 / abs(odds) if odds < 0 else odds / 100.0), 2)
                if res == "Loss": return -100.0
                return 0.0

            ml_odds = lo.home_moneyline if pred_margin > 0 else lo.away_moneyline

            # Use actual spread odds from consolidated table instead of hardcoded -110
            if rl_pick and rl_pick == home_abbr:
                ats_odds_val = lo.spread_home_odds if lo.spread_home_odds else -110
            else:
                ats_odds_val = lo.spread_away_odds if lo.spread_away_odds else -110

            ats_profit_val = _pl(rl_res, ats_odds_val)
            ou_profit_val = _pl(ou_res, -110)
            ml_profit_val = _pl(ml_res, ml_odds) if ml_res else None

            # Calibrate confidence to empirical win rate, then compute EV per $100 flat bet
            def _ev(conf: float, odds: int | None) -> float:
                if not odds or conf <= 0 or conf >= 1:
                    return 0.0
                profit = (100.0 * 100.0 / float(abs(odds))) if odds < 0 else float(odds)
                return round((conf * profit) - ((1.0 - conf) * 100.0), 2)

            cal_ats = calibrate(ats_conf, "rl", sport="mlb")
            cal_ou = calibrate(ou_conf, "ou", sport="mlb")
            cal_ml = calibrate(ml_conf, "ml", sport="mlb")
            ats_ev_val = _ev(cal_ats, ats_odds_val)
            ou_ev_val = _ev(cal_ou, -110)
            ml_ev_val = _ev(cal_ml, ml_odds) if ml_odds else None

            await conn.execute("""
                INSERT INTO mlb.game_predictions (
                    game_id, source,
                    predicted_home_runs, predicted_away_runs, predicted_total, predicted_margin,
                    rl_conf, ou_conf, ml_conf, margin_conf,
                    run_line_pick, ou_pick, ml_pick,
                    actual_home_runs, actual_away_runs, actual_total, actual_margin,
                    run_line_result, ou_result, ml_result,
                    ats_odds, ou_odds, ml_odds, ats_profit, ou_profit, ml_profit,
                    ats_ev, ou_ev, ml_ev
                ) VALUES ($1,'api',
                    $2,$3,$4,$5,
                    $22,$23,$24,$25,
                    $6,$7,$8,
                    $9,$10,$11,$12,
                    $13,$14,$15,
                    $16,$17,$18,$19,$20,$21,
                    $26,$27,$28
                ) ON CONFLICT (game_id, source) DO NOTHING
            """,
                    g["id"], home_runs, away_runs, pred_total, pred_margin,
                    rl_pick,
                    "Over" if lo.over_under is not None and pred_total > lo.over_under else ("Under" if lo.over_under is not None else None),
                    "home" if pred_margin > 0 else "away",
                    hs, aws, atotal, am,
                    rl_res, ou_res, ml_res,
                    ats_odds_val, -110, ml_odds,
                    ats_profit_val, ou_profit_val, ml_profit_val,
                    ats_conf, ou_conf, ml_conf, ats_conf,
                    ats_ev_val, ou_ev_val, ml_ev_val
                )

            # Track
            if rl_res == "Win": rl_c += 1
            elif rl_res == "Loss": rl_i += 1
            elif rl_res == "Push": rl_p += 1
            if ou_res == "Win": ou_c += 1
            elif ou_res == "Loss": ou_i += 1
            elif ou_res == "Push": ou_p += 1
            if ml_res == "Win": ml_c += 1
            elif ml_res == "Loss": ml_i += 1

            if (idx + 1) % 25 == 0:
                logger.info(f"  [{idx+1}/{total}] saved {rl_c+rl_i+rl_p} games")

        rl_t = rl_c + rl_i
        ou_t = ou_c + ou_i
        ml_t = ml_c + ml_i

        db_results = {
            "season": year,
            "total_games": rl_t,
            "run_line": {"correct": rl_c, "incorrect": rl_i, "pushes": rl_p,
                          "pct": round(rl_c / max(rl_t, 1), 3)},
            "over_under": {"correct": ou_c, "incorrect": ou_i, "pushes": ou_p,
                            "pct": round(ou_c / max(ou_t, 1), 3)},
            "moneyline": {"correct": ml_c, "incorrect": ml_i,
                           "pct": round(ml_c / max(ml_t, 1), 3)},
        }

        logger.info(f"\nBacktest {year} complete!")
        logger.info(f"  RL: {rl_c}/{rl_t} = {db_results['run_line']['pct']*100:.1f}%")
        logger.info(f"  OU: {ou_c}/{ou_t} = {db_results['over_under']['pct']*100:.1f}%")
        logger.info(f"  ML: {ml_c}/{ml_t} = {db_results['moneyline']['pct']*100:.1f}%")

        return db_results
    finally:
        await conn.close()

