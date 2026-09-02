"""Raw-material loaders for X post content.

Each loader returns *structured, honest seeds derived from real DB rows* — never
fabricated. The composer turns a seed into editable draft text + a traceable
source_ref (loader + sport + game/pick source ids) so a published post can always be
traced back to the exact row(s) it quotes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_SPORTS = ("mlb", "nba", "nfl")


def _fmt_edge_pct(edge) -> str:
    try:
        return f"+{float(edge) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


# ----------------------------------------------------------------------------- best pick
async def load_best_picks(db: AsyncSession, *, limit: int = 5, horizon_days: int = 14) -> list[dict]:
    """Return the top-value upcoming picks across sports — the SAME single-highest-edge
    leg per game and edge ranking the earlknowsball.com home 'best bets' uses.

    Reuses home.py's SQL factory + edge picker so a tweeted pick is exactly what the
    site shows. Returns seeds with kind='best_pick', a human bundle, and source_ref
    that includes the per-sport game id + the winning leg (day-of-game dates expire).
    """
    from app.routers.home import _build_best_bets_sql, _best_leg, _fix_decimals

    limit = max(1, min(int(limit), 10))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)

    candidates: list[dict] = []
    for schema, kind in (("mlb", "mlb"), ("nba", "nba"), ("nfl", "nfl")):
        try:
            sql = _build_best_bets_sql(schema, kind)
            rows = (
                await db.execute(text(sql), {"now": now, "horizon": horizon, "limit": 30})
            ).mappings().all()
            for r in rows:
                g = _fix_decimals(dict(r))
                if r.get("pick_ats_ev") is not None:
                    g.setdefault("pick_ats_ev", r.get("pick_ats_ev"))
                best = _best_leg(g)
                if best is None or best["edge"] is None or best["edge"] <= 0:
                    continue
                bt = best["type"]
                if bt == "ats":
                    label, ev = g.get("pick_spread"), g.get("pick_ats_ev")
                elif bt == "ou":
                    label, ev = g.get("pick_over_under"), g.get("pick_ou_ev")
                else:  # ml
                    label, ev = g.get("pick_moneyline"), g.get("pick_ml_ev")
                if label in (None, "", "Push / No edge"):
                    continue
                edge = best["edge"]
                conf_pct = round((best["conf"] or 0) * 100, 1)
                seed = {
                    "kind": "best_pick",
                    "sport": kind,
                    "game_date": str(g.get("game_date")),
                    # keep below 280 chars when combined; safe target ~220
                    "text": (
                        f"Best value today from {kind.upper()}: "
                        f"{g.get('away_team') or 'AWAY'} @ {g.get('home_team') or 'HOME'} "
                        f"→ {label}"
                        + (f" (edge {_fmt_edge_pct(edge)})" if edge else "")
                    ).strip(),
                    "source_ref": {
                        "loader": "load_best_picks",
                        "sport": kind,
                        "game_id": g.get("id"),
                        "game_date": str(g.get("game_date")),
                        "home_team": g.get("home_team"),
                        "away_team": g.get("away_team"),
                        "leg": bt,
                        "label": label,
                        "edge": edge,
                        "conf_pct": conf_pct,
                        "ev": ev,
                        "game_dt": str(g.get("date")),
                    },
                }
                candidates.append(seed)
        except Exception as exc:  # noqa: BLE001 - one sport must not kill the rest
            log.warning("best-pick source failed for %s: %s", kind, exc)

    candidates.sort(key=lambda s: s["source_ref"].get("edge") or 0, reverse=True)
    return candidates[:limit]


# ----------------------------------------------------------------------------- record
async def load_record_update(db: AsyncSession, *, season_year: int | None = None) -> list[dict]:
    """Honest settled-record rollup per sport (win + loss), for the transparency post.

    Uses the per-sport settlement column directly:
      mlb: run_line_result  ('Win'/'Loss'/'Push')   nba/nfl: ats_result
    Only returns a seed for a sport with >0 settled (no fake '0-0').
    """
    sports = {"mlb": "run_line_result", "nba": "ats_result", "nfl": "ats_result"}
    seeds: list[dict] = []
    # Resolve the season year up front (latest season per sport when None) so SQL
    # always binds a concrete integer year — avoids asyncpg $1 ambiguity entirely.
    resolved_years = {}
    for sport in sports:
        if season_year:
            resolved_years[sport] = int(season_year)
            continue
        try:
            yrow = (await db.execute(
                text(f"SELECT year FROM {sport}.seasons ORDER BY year DESC LIMIT 1")
            )).scalar()
            resolved_years[sport] = int(yrow) if yrow else None
        except Exception as exc:  # noqa: BLE001
            log.warning("latest-season lookup failed for %s: %s", sport, exc)
            resolved_years[sport] = None

    for sport, result_col in sports.items():
        year = resolved_years.get(sport)
        if year is None:
            continue
        try:
            sql = text(
                f"""
                SELECT
                  count(*) FILTER (WHERE lower(gp.{result_col}) = 'win')
                    AS wins,
                  count(*) FILTER (WHERE lower(gp.{result_col}) = 'loss')
                    AS losses,
                  count(*) FILTER (WHERE lower(gp.{result_col}) = 'push')
                    AS pushes
                FROM {sport}.games g
                JOIN {sport}.game_predictions gp ON gp.game_id = g.id
                WHERE lower(g.status::text) IN ('final','complete')
                  AND g.season_id = (SELECT id FROM {sport}.seasons WHERE year = :y)
                  AND gp.source = 'api'
                """
            )
            row = (await db.execute(sql, {"y": year})).mappings().first()
            if row is None:
                continue
            w, l, p = int(row["wins"] or 0), int(row["losses"] or 0), int(row["pushes"] or 0)
            if w + l + p == 0:
                continue  # no settled api picks yet this season
            pct = f"{100.0 * w / (w + l):.1f}%" if (w + l) else "n/a"
            seeds.append({
                "kind": "record_update",
                "sport": sport,
                "text": (
                    f"@earl_knows_ball {sport.upper()} ATS record ({year}): {w}-{l}"
                    + (f"-{p}" if p else "")
                    + f" ({pct}). We post the losses too."
                ).strip(),
                "source_ref": {
                    "loader": "load_record_update",
                    "sport": sport,
                    "season_year": year,
                    "wins": w, "losses": l, "pushes": p,
                    "settlement_col": result_col,
                },
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("record source failed for %s: %s", sport, exc)
    return seeds


LOADERS = {
    "best_pick": load_best_picks,
    "record_update": load_record_update,
}

CONTENT_TYPES = {
    "best_pick": {
        "label": "Best value pick",
        "desc": "Highest-edge pick among today's upcoming games (same as site best-bets).",
        "loader": "best_pick",
    },
    "record_update": {
        "label": "Record / transparency update",
        "desc": "Auto rollup of settled ATS record — wins and losses.",
        "loader": "record_update",
    },
}
