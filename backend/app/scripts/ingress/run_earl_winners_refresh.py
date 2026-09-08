#!/usr/bin/env python3
"""
Earl's Winners refresh — standalone subprocess job.

Refreshes the cross-sport "Earl's Winners 🏆" snapshot (`public.earl_winners`)
shown on the home + sport home pages.

WHAT A "WINNER" IS (ground truth, single source):
  A winner = a settled FINAL game's prediction row whose per-market result
  column equals 'Win'. We DO NOT recompute result math here — the settle passes
  (app.handicapping.<sport>.settle_predictions / update_prediction_results)
  already wrote `*_result`/`*_profit`/`*_odds` for FINAL games. This module only
  READS those already-trusted values. That guarantees Earl's Winners can never
  disagree with what the schedule cards / recap show.
      - market  spread:  result col run_line_result (mlb) | ats_result (nfl,nba)
      - market  total:   ou_result     (all)
      - market  ml:      ml_result     (all)
  Winner semantics: result='Win', ANY source (api OR backtest) — per Rich, do
  NOT filter by source. 'Push' and 'Loss' are excluded (not a braggable win).

CADENCE RULE (the "every ~10 new, but ≤ once per day" gate):
  - The block must stay pin-stable within a Chicago calendar day.
  - A (re)materialize happens only when BOTH hold:
      (1) there are >= MIN_NEW_WINS (10) winners that settled AFTER the most
          recent pick already in the snapshot (i.e. genuinely new since last
          rotate), on a FINAL game, AND
      (2) the snapshot has not been rotated today (America/Chicago).
  - We store the last rotate date + last max game_date in the snapshot itself
    (max refreshed_at) so "new since last time" is self-describing & idempotent.

The snapshot holds the most recent N (SNAPSHOT_KEEP) settled winners across all
three sports, newest first (fine-grained tiebreak: profit desc). The sport home
pages filter to their sport; the `all` home page shows the whole set.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_earl_winners_refresh.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ensure .env (backend/.env) is loaded before importing app.* config
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

import asyncpg  # noqa: E402

from app.scripts.ingress._ingest_common import report_task_outcome  # noqa: E402
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("earl.winners_refresh")

TASK_NAME = "earl-winners-refresh"
MIN_NEW_WINS = 10      # rotate only when >= this many NEWLY-settled +EV wins
SNAPSHOT_KEEP = 24     # winners held in the block at once
MIN_EV = 0.0           # only showcase picks that were +EV recommendations at tip
                       # (ev = *_ev model expected value, profit $/100 stake)
# Window: only include settled winners from the last MAX_AGE_DAYS for the block
# (Rich 2026-09-06: look-back = 1 week). Preseason excluded separately in _sport_sql.
MAX_AGE_DAYS = 7
CHICAGO_TZ = timezone(timedelta(hours=-5))  # America/Chicago (CDT); DST handled below

# Uniform result-column aliases as emitted by _sport_sql (per-sport real cols
# are mapped there). spread -> 'spread_result'; total -> ou_result; ml -> ml_result.
SPORT_RESULT_COLS = {
    "mlb": {"spread": "spread_result", "total": "ou_result", "ml": "ml_result"},
    "nfl": {"spread": "spread_result", "total": "ou_result", "ml": "ml_result"},
    "nba": {"spread": "spread_result", "total": "ou_result", "ml": "ml_result"},
}


def _chicago_today() -> date:
    """America/Chicago calendar date (CDT = UTC-5 in summer, CST = UTC-6).
    DST edge: compute via zoneinfo if available, else naive CDT offset."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:  # pragma: no cover - fallback
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


# Columns to read per sport per market. OU pick is lower-case text ("over 46.5").
def _sport_sql(schema: str) -> str:
    """Return the per-sport SELECT that reads settled FINAL winners into a
    uniform shape. Column aliases are the uniform winner shape below."""
    schemap = f"{schema}."
    spread_result = "run_line_result" if schema == "mlb" else "ats_result"
    spread_pick = "run_line_pick" if schema == "mlb" else "spread_pick"
    # odds/profit for the run-line/spread market are uniformly ats_* on ALL sports
    # (only the pick + result columns are run_line_* for mlb).
    return f"""
    SELECT
        '{schema}'                                 AS sport,
        g.id                                       AS game_id,
        gp.{spread_pick}                           AS spread_pick,
        gp.ou_pick                                 AS ou_pick,
        gp.ml_pick                                 AS ml_pick,
        ht.abbreviation                            AS home_abbrev,
        at.abbreviation                            AS away_abbrev,
        g.home_score                               AS home_score,
        g.away_score                               AS away_score,
        (g.date AT TIME ZONE 'America/New_York')::date   AS game_date,
        blc.closing_ou                             AS closing_ou,
        gp.{spread_result}                         AS spread_result,
        gp.ou_result                               AS ou_result,
        gp.ml_result                               AS ml_result,
        gp.ats_odds                                AS ats_odds,
        gp.ou_odds                                 AS ou_odds,
        gp.ml_odds                                 AS ml_odds,
        gp.ats_profit                              AS ats_profit,
        gp.ou_profit                               AS ou_profit,
        gp.ml_profit                               AS ml_profit,
        gp.ats_ev                                  AS ats_ev,
        gp.ou_ev                                  AS ou_ev,
        gp.ml_ev                                  AS ml_ev
    FROM {schemap}game_predictions gp
    JOIN {schemap}games g        ON gp.game_id = g.id
    JOIN {schemap}teams ht       ON ht.id = g.home_team_id
    JOIN {schemap}teams at       ON at.id = g.away_team_id
    LEFT JOIN {schemap}betting_lines_consolidated blc ON gp.game_id = blc.game_id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
      -- NO PRESEASON: NFL/NBA store REG/PRE/POST/PLAYIN in game_type; MLB uses R etc.
      -- Excluding game_type='PRE' keeps preseason games out of Earl's Winners.
      AND g.game_type IS DISTINCT FROM 'PRE'
    """


def _market_winner(row, market: str):
    """Given one settled prediction row, return a uniform winner dict for the
    given market if it cashed (result='Win'), else None.

    Returns keys: sport, game_id, market, pick_text, odds_at_tip, profit,
    home_team, away_team, home_score, away_score, game_date, winning_side.
    """
    sport = row["sport"]
    rc = SPORT_RESULT_COLS[sport][market]
    if row.get(rc) != "Win":
        return None

    home_ab = (row.get("home_abbrev") or "").strip()
    away_ab = (row.get("away_abbrev") or "").strip()
    # `spread_pick` here is a normalized alias = the full spread pick text
    # (mlb: run_line_pick like 'HOU -1.5'; nfl/nba: spread_pick like 'BOS -5.5').
    spread_pick = (row.get("spread_pick") or "").strip()

    base = {
        "sport": sport,
        "game_id": row["game_id"],
        "home_team": home_ab,
        "away_team": away_ab,
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "game_date": row.get("game_date"),
    }
    if row.get("game_date") is None:
        return None

    if market == "spread":
        # pick_text like 'HOU -1.5' — the normalized spread_pick alias above.
        if not spread_pick:
            return None
        tokens = spread_pick.upper().split()
        picked_ab = tokens[0] if tokens else ""
        side = "home" if picked_ab == home_ab and home_ab else ("away" if picked_ab == away_ab and away_ab else "")
        if not side:
            return None
        return {**base, "market": "spread", "pick_text": spread_pick,
                "odds_at_tip": _fmt_odds(row.get("ats_odds")),
                "profit": row.get("ats_profit"), "ev": row.get("ats_ev"),
                "winning_side": side}
    if market == "total":
        ou = (row.get("ou_pick") or "").strip().lower()
        if not (ou.startswith("over") or ou.startswith("under")):
            return None
        over = ou.startswith("over")
        word = "Over" if over else "Under"
        # Preferred: show book's OU line e.g. "Over 8.5" (from betting_lines closing_ou).
        line = row.get("closing_ou")
        pick_txt = f"{word} {line}" if line is not None else f"{word} {ou[len('over'):].strip()}".strip()
        return {**base, "market": "total", "pick_text": pick_txt,
                "odds_at_tip": _fmt_odds(row.get("ou_odds")),
                "profit": row.get("ou_profit"), "ev": row.get("ou_ev"),
                "winning_side": "over" if over else "under"}
    if market == "ml":
        ml = (row.get("ml_pick") or "").strip().upper()
        if not ml:
            return None
        picked_ab = ml.split()[0] if ml.split() else ml
        side = "home" if picked_ab == home_ab and home_ab else ("away" if picked_ab == away_ab and away_ab else None)
        if not side:
            return None
        return {**base, "market": "ml", "pick_text": f"{picked_ab} ML",
                "odds_at_tip": _fmt_odds(row.get("ml_odds")),
                "profit": row.get("ml_profit"), "ev": row.get("ml_ev"),
                "winning_side": side}
    return None


def _fmt_odds(v):
    """American odds display: store raw signed int (e.g. -110, +150)."""
    if v is None:
        return None
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return None


async def refresh_winners(started_at=None) -> dict:
    """Rebuild Earl's Winners snapshot subject to the cadence + threshold gate.

    Returns a summary dict for logging / task outcome.
    """
    if started_at is None:
        started_at = datetime.now(timezone.utc)

    conn = await asyncpg.connect(PSYCOPG2_DATABASE_URL)
    summary = {"new_wins_seen": 0, "rotated": False, "snapshot_rows": 0, "reason": ""}
    try:
        # ---- 1) current state of the snapshot ----
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(refreshed_at), DATE '2000-01-01') AS last_rotate, "
            "       count(*) AS rows_now, "
            "       COALESCE(MAX(game_date), DATE '2000-01-01') AS last_game_date "
            "FROM public.earl_winners"
        )
        last_rotate = row["last_rotate"]
        last_game_date = row["last_game_date"] or date(2000, 1, 1)
        today = _chicago_today()

        # (2) skip if already rotated today
        if last_rotate >= today:
            summary["reason"] = f"already rotated today ({last_rotate})"
            return summary

        # ---- 3) collect settled winners per sport / market ----
        # A "showcase winner" = a cashed pick that was a genuine +EV model
        # recommendation at tip AND settled recently enough to feel current.
        cutoff = today - timedelta(days=MAX_AGE_DAYS)
        winners: list[dict] = []
        for schema in ("mlb", "nba", "nfl"):
            sql = _sport_sql(schema)
            rows = await conn.fetch(sql)
            for r in rows:
                for market in ("spread", "total", "ml"):
                    w = _market_winner(r, market)
                    if w is None:
                        continue
                    ev = w.get("ev")
                    # Skip picks that were not positive-EV recommendations, and
                    # any without an EV value (can't vouch for it).
                    if ev is None or ev <= MIN_EV:
                        continue
                    # Skip winners older than the retention window.
                    if w["game_date"] < cutoff:
                        continue
                    winners.append(w)

        # The gate counts only wins strictly NEWER than the last snapshot game-date
        # (so a stale empty block eventually rotates in even when MLB/NFL/NBA slow).
        summary["new_wins_seen"] = sum(1 for w in winners if w["game_date"] > last_game_date)

        # 4) gate: need >= MIN_NEW_WINS new +EV wins since the last rotated game
        #    date, otherwise keep the block as-is (truthful & stable).
        if summary["new_wins_seen"] < MIN_NEW_WINS:
            summary["reason"] = (f"only {summary['new_wins_seen']} new +EV wins "
                                 f"(need >= {MIN_NEW_WINS}) since {last_game_date}")
            return summary

        # 5) rotate: showcase the MOST RECENT cashed wins first
        #    (Rich 2026-09-06: prioritize RECENCY over EV — the block is "these
        #    just hit", not "our all-time biggest edges"). EV is still stored and
        #    shown on the card, but the ORDER is newest-game-first; EV is a tiebreak
        #    for two winners from the same day (higher EV surfaces first).
        #    Non-preseason only + 7-day look-back already applied above.
        winners.sort(key=lambda w: (w["game_date"], w.get("ev") or 0), reverse=True)
        keep = winners[:SNAPSHOT_KEEP]

        await conn.execute("DELETE FROM public.earl_winners")
        for i, w in enumerate(keep):
            await conn.execute(
                """INSERT INTO public.earl_winners
                     (sport, game_id, market, pick_text, odds_at_tip, profit, ev,
                      home_team, away_team, home_score, away_score, game_date,
                      winning_side, sort_key, refreshed_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                   ON CONFLICT (sport, game_id, market) DO NOTHING""",
                w["sport"], w["game_id"], w["market"], w["pick_text"],
                w["odds_at_tip"], w["profit"], w.get("ev"), w["home_team"],
                w["away_team"], w["home_score"], w["away_score"], w["game_date"],
                w["winning_side"], i, today,
            )
        summary.update({"rotated": True, "snapshot_rows": len(keep),
                        "reason": f"rotated {len(keep)} most-recent winners (newest-first)"})
        return summary
    finally:
        await conn.close()


async def main() -> int:
    started_at = datetime.now(timezone.utc)
    ok = False
    err = ""
    try:
        summary = await refresh_winners(started_at)
        ok = summary["rotated"]  # a (correct) no-op is still success - task didn't fail
        logger.info("Winners refresh summary: %s", summary)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        logger.exception("Winners refresh FAILED")
        ok = False
    await report_task_outcome(TASK_NAME, ok, err, started_at)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
