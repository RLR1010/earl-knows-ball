"""
Build the empirical same-game correlation table from settled game predictions.

Replaces/augments the V1 heuristic correlation flags with ACTUAL joint-hit
rates computed from stored `*_result` columns (ml_result / ou_result /
ats_result). This is the honest-numbers version: instead of assuming
"favorite ML + Over are correlated", we measure P(both hit) vs P(ML)·P(OU)
across every settled game and store the delta.

Table: <sport>.correlations
  id        serial PK
  pair_key  text  UNIQUE   e.g. 'ml_fav:total_over' or 'ml_fav:ml_fav' (same type)
  kind_a    text           'ml'|'spread'|'total' + '_fav'/'_dog' side marker for ml
  kind_b    text
  n         int            # settled games with both legs present
  p_a       float          marginal win rate of leg A
  p_b       float          marginal win rate of leg B
  p_joint   float          empirical P(both hit)
  p_indep   float          p_a * p_b (independence assumption)
  corr      float          p_joint - p_indep  (+= correlated, -= negatively corr)
  is_block  bool           true if near-duplicate (e.g. ml_fav + spread_fav same side)
  note      text
  built_at  timestamptz

Run:  cd backend && PYTHONPATH=. venv/bin/python app/scripts/build_parlay_correlations.py [--apply]
      (--apply writes rows; default prints what it WOULD store)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)

from app.database import async_engine, async_session  # noqa: E402


# leg category key -> (label, wincol, wintok, is_push_half)
# We categorize ML legs into favorite/dog based on the side the model actually
# favors (lower ml_odds). Spread legs are 'spread' (any side); total legs
# over/under.
TOTAL_HALF = {"Win": 1.0, "Loss": 0.0, "Push": 0.5}


def _settle_sql(sport: str, ids: bool) -> str:
    """Build the base query returning one row per settled game with leg outcomes.

    ids=True → match ml_pick against g.home_team_id/g.away_team_id (NBA).
    ids=False → match against teams.abbreviation (MLB/NFL).
    """
    side_expr = (
        "CASE WHEN p.ml_pick::text = g.home_team_id::text THEN 'home' "
        "      WHEN p.ml_pick::text = g.away_team_id::text THEN 'away' "
        "      ELSE NULL END"
        if ids
        else "CASE WHEN p.ml_pick = ht.abbreviation THEN 'home' "
        "      WHEN p.ml_pick = at.abbreviation THEN 'away' "
        "      ELSE NULL END"
    )
    fav_expr = (
        # favorite = the side with negative (or lower) ML odds
        "CASE WHEN p.ml_odds IS NULL THEN NULL "
        "      WHEN p.ml_odds < 0 THEN "
        f" ({side_expr}) "
        "      ELSE "
        "   CASE WHEN "
        f"({side_expr})='home' "
        "   THEN 'away' ELSE 'home' END END"
    )
    spread_col = "run_line_pick" if sport == "mlb" else "spread_pick"
    spread_result = "run_line_result" if sport == "mlb" else "ats_result"

    return f"""
        SELECT
            p.game_id,
            p.ml_pick,
            {side_expr}        AS ml_side,
            {fav_expr}         AS fav_side,
            p.ml_result,
            -- spread: does the model's pick side win? (we just need a Win/Loss/Push)
            p.{spread_col} AS spread_pick,
            p.{spread_result} AS spread_result,
            p.ou_pick,
            p.ou_result
        FROM {sport}.game_predictions p
        JOIN {sport}.games g ON g.id = p.game_id
        LEFT JOIN {sport}.teams ht ON ht.id = g.home_team_id
        LEFT JOIN {sport}.teams at ON at.id = g.away_team_id
    """


def ml_cat(side: str, fav: str) -> str:
    """Side ('home'/'away') + fav_side -> 'ml_fav' / 'ml_dog'."""
    if not side or not fav:
        return "ml"
    return "ml_fav" if side == fav else "ml_dog"


def sp_cat(pick: str, side: str) -> str:
    """Spread pick -> leg category. We treat all spread legs as 'spread' for
    correlation with totals; a spread leg paired with an ML leg on the SAME
    team is marked as a block."""
    return "spread"


def collect_buckets(rows, is_mlb: bool):
    """Return {(cat_a, cat_b): {'a_win':[], 'b_win':[], 'n':int}} for same-game
    pairs (all pairs of ml/spread/total legs present in the same game)."""
    buckets: dict = {}
    for r in rows:
        ml_side = r["ml_side"]
        fav = r["fav_side"]
        ml_result = r["ml_result"]
        ou_pick = (r["ou_pick"] or "").lower()
        ou_result = r["ou_result"]
        sp_pick = r["spread_pick"]
        sp_result = r["spread_result"]

        # -- build the set of legs present for this game with a win probability --
        legs = {}
        # ML leg
        if ml_side and ml_result in TOTAL_HALF:
            legs["ml"] = (ml_cat(ml_side, fav), TOTAL_HALF[ml_result], ml_side)
        # total leg
        if ou_pick in ("over", "under") and ou_result in TOTAL_HALF:
            legs["total"] = (f"total_{ou_pick}", TOTAL_HALF[ou_result], None)
        # spread leg
        if sp_pick and sp_result in TOTAL_HALF:
            legs["spread"] = ("spread", TOTAL_HALF[sp_result], sp_pick)

        # pairwise combos within the same game — only ML×total (the ones we care
        # about for correlation) plus the ML×spread block check.
        keys = list(legs.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                ka = keys[i]
                kb = keys[j]
                # skip spread×spread and total×total (impossible in one game)
                if ka == kb:
                    continue
                ca, wa, sa = legs[ka]
                cb, wb, sb = legs[kb]
                pair = tuple(sorted((ca, cb)))
                if pair not in buckets:
                    buckets[pair] = {"a_win": [], "b_win": [], "n": 0}
                buckets[pair]["a_win"].append(wa)
                buckets[pair]["b_win"].append(wb)
                buckets[pair]["n"] += 1

    return buckets


async def build(sport: str, apply: bool = False) -> list:
    ids = sport == "nba"
    sql = _settle_sql(sport, ids=ids)
    async with async_session() as db:
        result = await db.execute(text(sql))
        rows = list(result.mappings())
        buckets = collect_buckets(rows, is_mlb=(sport == "mlb"))

        out = []
        for (ca, cb), st in sorted(buckets.items()):
            n = st["n"]
            if n < 30:
                continue
            p_a = sum(st["a_win"]) / n
            p_b = sum(st["b_win"]) / n
            p_joint = sum(a * b for a, b in zip(st["a_win"], st["b_win"])) / n
            p_indep = p_a * p_b
            corr = p_joint - p_indep
            is_block = (ca.startswith("ml") and cb == "spread")
            note = "near-duplicate same-team ML+spread" if is_block else ""
            rec = {
                "pair_key": f"{ca}:{cb}",
                "kind_a": ca,
                "kind_b": cb,
                "n": n,
                "p_a": round(p_a, 4),
                "p_b": round(p_b, 4),
                "p_joint": round(p_joint, 4),
                "p_indep": round(p_indep, 4),
                "corr": round(corr, 4),
                "is_block": bool(is_block),
                "note": note,
            }
            out.append(rec)

        # ensure table exists
        await db.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {sport}.correlations (
                id serial PRIMARY KEY,
                pair_key text NOT NULL UNIQUE,
                kind_a text NOT NULL,
                kind_b text NOT NULL,
                n integer NOT NULL,
                p_a double precision NOT NULL,
                p_b double precision NOT NULL,
                p_joint double precision NOT NULL,
                p_indep double precision NOT NULL,
                corr double precision NOT NULL,
                is_block boolean NOT NULL DEFAULT false,
                note text,
                built_at timestamptz NOT NULL DEFAULT now()
            )
        """))

        if apply:
            for rec in out:
                await db.execute(text(f"""
                    INSERT INTO {sport}.correlations
                        (pair_key, kind_a, kind_b, n, p_a, p_b, p_joint, p_indep, corr, is_block, note, built_at)
                    VALUES (:pair_key, :kind_a, :kind_b, :n, :p_a, :p_b, :p_joint, :p_indep, :corr, :is_block, :note, now())
                    ON CONFLICT (pair_key) DO UPDATE SET
                        n=EXCLUDED.n, p_a=EXCLUDED.p_a, p_b=EXCLUDED.p_b,
                        p_joint=EXCLUDED.p_joint, p_indep=EXCLUDED.p_indep,
                        corr=EXCLUDED.corr, is_block=EXCLUDED.is_block,
                        note=EXCLUDED.note, built_at=now()
                """), rec)
        await db.commit()
        return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["mlb", "nfl", "nba"], default=None,
                        help="sport to build; default = all")
    parser.add_argument("--apply", action="store_true",
                        help="write rows to DB (default: dry-run print)")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else ["mlb", "nfl", "nba"]
    for sport in sports:
        print(f"\n=== {sport} ===")
        recs = await build(sport, apply=args.apply)
        if not recs:
            print("  (no pairs with n>=30)")
        for r in recs:
            flag = "BLOCK" if r["is_block"] else ("warn" if abs(r["corr"]) >= 0.02 else "  ok ")
            print(f"  {r['pair_key']:22s} n={r['n']:5d}  P(A)={r['p_a']:.3f} P(B)={r['p_b']:.3f} "
                  f"JOINT={r['p_joint']:.3f} PROD={r['p_indep']:.3f} corr={r['corr']:+.3f}  {flag}")
        if not args.apply:
            print("  (dry-run; re-run with --apply to write)")


if __name__ == "__main__":
    asyncio.run(main())
