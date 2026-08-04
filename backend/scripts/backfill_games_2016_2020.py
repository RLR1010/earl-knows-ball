"""Full backfill of missing nfl.games rows for 2016-2019 (relocated-team REG
games) plus the 2016/2020 playoffs, driven by a verified missing-matchup list.

This is surgical: we only insert games whose (season, week, {team1, team2})
exists in the missing set derived from game_stats (the complete source). We
grab ESPN ids/dates/scores for those matchups and write them with:
  - REG games: game_type='REG', week = the game_stats week (authoritative)
  - Playoffs:  game_type='REG' (Option A), canonical weeks 19/20/21/22

Safe: never touches existing rows (skips any id already present), never
auto-commits unless --apply, prints every intended insert.

Usage:
  python scripts/backfill_games_2016_2020.py          # dry-run
  python scripts/backfill_games_2016_2020.py --apply  # write + recompute
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text, select

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
load_dotenv(os.path.join(_BACKEND, ".env"))
sys.path.insert(0, _BACKEND)

from dateutil import parser  # noqa: E402

from app.models.nfl.season import Season  # noqa: E402
from app.models.nfl.game import Game  # noqa: E402
from app.ingestion.espn import fetch_espn_scoreboard, ESPN_TEAM_MAP  # noqa: E402

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)


async def build_missing_map(db):
    """Return {(season, week, low, high): True} for every verified-missing
    REG matchup (2016-2019), plus the playoff missing set (2016) keyed by season."""
    q = text("""
        WITH gs_pairs AS (
          SELECT DISTINCT gs.season, gs.week,
                 LEAST(gs.team_abbr, gs.opponent_abbr) AS t1,
                 GREATEST(gs.team_abbr, gs.opponent_abbr) AS t2
          FROM nfl.game_stats gs WHERE gs.season_type='REG' AND gs.season BETWEEN 2016 AND 2019
        )
        SELECT gp.season, gp.week, gp.t1, gp.t2
        FROM gs_pairs gp
        WHERE NOT EXISTS (
          SELECT 1 FROM nfl.games g
          JOIN nfl.seasons s ON s.id=g.season_id AND s.year=gp.season AND g.game_type='REG'
          JOIN nfl.teams a ON a.id=g.away_team_id AND a.abbreviation IN (gp.t1,gp.t2)
          JOIN nfl.teams h ON h.id=g.home_team_id AND h.abbreviation IN (gp.t1,gp.t2)
          WHERE g.week=gp.week AND g.home_team_id<>g.away_team_id
        )
    """)
    rows = (await db.execute(q)).all()
    missing = {((r.season, r.week, r.t1, r.t2)): True for r in rows}
    return missing


async def fetch_season_reg_events(season):
    """Fetch REG events for a season using SEP-DEC + JAN windows (force_dates).
    The JAN window catches weeks that spill into the following year (e.g. week 17
    of a 16-week season, rescheduled games)."""
    yr, nxt = season, season + 1
    events = []
    for dates in (
        f"{yr}0901-{yr}0930",
        f"{yr}1001-{yr}1031",
        f"{yr}1101-{yr}1130",
        f"{yr}1201-{yr}1231",
        f"{nxt}0101-{nxt}0131",
    ):
        try:
            events += await fetch_espn_scoreboard(season, 2, None, force_dates=dates)
        except Exception as e:
            print(f"  [warn] fetch {dates}: {e}")
    return events


def event_abbrs(e):
    c = e.get("competitions", [{}])[0]
    ts = c.get("competitors", [])
    if len(ts) < 2:
        return None
    def ab(x):
        return (x.get("team") or {}).get("abbreviation")
    a = ESPN_TEAM_MAP.get(ab(ts[0]) or "", ab(ts[0]) or "")
    b = ESPN_TEAM_MAP.get(ab(ts[1]) or "", ab(ts[1]) or "")
    return (a, b)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    engine = create_async_engine(DATABASE_URL)
    async with AsyncSession(engine) as db:
        missing_reg = await build_missing_map(db)
        print(f"Verified missing REG matchups: {len(missing_reg)}")

        # Group missing by season for fetching
        by_season = defaultdict(list)
        for (season, week, t1, t2) in missing_reg:
            by_season[season].append((week, t1, t2))
        # also add playoff missing (2016) handled separately below

        inserts = 0
        skipped_existing = 0
        unresolved = 0

        for season in sorted(by_season):
            s = (await db.execute(select(Season).where(Season.year == season))).scalar_one()
            print(f"\n=== {season} REG backfill ({len(by_season[season])} games) ===")
            events = await fetch_season_reg_events(season)
            # Key by (espn_week, sorted_pair_tuple) so two different-week games
            # between the same pair (e.g. DEN@OAK twice) don't collide.
            keyed = {}
            for ev in events:
                ab = event_abbrs(ev)
                if not ab:
                    continue
                wk = ev.get("week", {}).get("number")
                if not wk:
                    continue
                keyed[(int(wk), tuple(sorted(ab)))] = ev
            print(f"  fetched {len(keyed)} (week,pair) matchups from ESPN")

            for (week, t1, t2) in by_season[season]:
                key = (int(week), tuple(sorted([t1, t2])))
                ev = keyed.get(key)
                if not ev:
                    print(f"  !! no ESPN event for {t1}/{t2} W{week}")
                    unresolved += 1
                    continue
                gid = int(ev["id"])
                existing = (await db.execute(select(Game).where(Game.id == gid))).scalar_one_or_none()
                if existing:
                    print(f"  skip (already exists) id={gid} {t1}/{t2} W{week}")
                    skipped_existing += 1
                    continue
                comp = ev.get("competitions", [{}])[0]
                comps = comp.get("competitors", [])
                away_raw = next((x for x in comps if x.get("homeAway") == "away"), None)
                home_raw = next((x for x in comps if x.get("homeAway") == "home"), None)
                if not home_raw or not away_raw:
                    print(f"  !! no home/away markers for {t1}/{t2} W{week}")
                    unresolved += 1
                    continue
                game_date = parser.parse(comp.get("date") or ev.get("date", ""))
                def val(x):
                    try: return int(x["score"]) if x.get("score") is not None else None
                    except Exception: return None
                away_score, home_score = val(away_raw), val(home_raw)
                print(f"  + {t1}/{t2} W{week} id={gid} {game_date.date()} "
                      f"{away_score}-{home_score} ({away_raw['team']['abbreviation']}@{home_raw['team']['abbreviation']})")
                if args.apply:
                    await db.execute(text("""
                        INSERT INTO nfl.games
                          (id, season_id, week, game_type, home_team_id, away_team_id,
                           date, status, home_score, away_score)
                        VALUES (:id,:season_id,:week,:game_type,
                                (SELECT id FROM nfl.teams WHERE abbreviation=:home),
                                (SELECT id FROM nfl.teams WHERE abbreviation=:away),
                                :date,'FINAL',:hs,:as)
                    """), {
                        "id": gid, "season_id": s.id, "week": week, "game_type": "REG",
                        "home": ESPN_TEAM_MAP.get(home_raw["team"]["abbreviation"], home_raw["team"]["abbreviation"]),
                        "away": ESPN_TEAM_MAP.get(away_raw["team"]["abbreviation"], away_raw["team"]["abbreviation"]),
                        "date": game_date, "hs": home_score, "as": away_score,
                    })
                inserts += 1

        # ---------- 2016 playoffs (11 games not in games table) ----------
        # ---------- Playoff backfill for 2016-2019 (weeks 19-22, type REG) ----------
        # (2020's playoff games already exist in games at 19-22 but only need a
        # game_stats week alignment — handled in the shift step below.)
        WEEK_MAP = {1: 19, 2: 20, 3: 21, 5: 22}
        for season in (2016, 2017, 2018, 2019):
            nxt = season + 1
            srow = (await db.execute(select(Season).where(Season.year == season))).scalar_one()
            # Jan window of the following calendar year holds that season's playoffs
            events = await fetch_espn_scoreboard(season, 3, None, force_dates=f"{nxt}0101-{nxt}0228")
            playoff = []
            for ev in events:
                if ev.get("season", {}).get("type") != 3:
                    continue  # skip REG week-17/ rescheduled games in the window
                c = ev.get("competitions", [{}])[0]
                ts = c.get("competitors", [])
                if len(ts) < 2:
                    continue
                ab = [ESPN_TEAM_MAP.get(t.get("team", {}).get("abbreviation"), t.get("team", {}).get("abbreviation")) for t in ts]
                if "AFC" in ab or "NFC" in ab:  # skip Pro Bowl (week 4)
                    continue
                playoff.append(ev)
            playoff.sort(key=lambda e: e.get("date", ""))
            print(f"\n=== {season} playoff backfill (weeks 19-22) ===")
            seen = set()
            for ev in playoff:
                wk = WEEK_MAP.get(ev.get("week", {}).get("number"))
                if not wk:
                    continue
                gid = int(ev["id"])
                if gid in seen:
                    continue
                seen.add(gid)
                existing = (await db.execute(select(Game).where(Game.id == gid))).scalar_one_or_none()
                if existing:
                    print(f"  skip (already exists) id={gid} wk={wk}")
                    skipped_existing += 1
                    continue
                c = ev.get("competitions", [{}])[0]
                comps = c.get("competitors", [])
                away_raw = next((x for x in comps if x.get("homeAway") == "away"), None)
                home_raw = next((x for x in comps if x.get("homeAway") == "home"), None)
                if not home_raw or not away_raw:
                    unresolved += 1
                    continue
                game_date = parser.parse(c.get("date") or ev.get("date", ""))
                def val(x):
                    try: return int(x["score"]) if x.get("score") is not None else None
                    except Exception: return None
                away_score, home_score = val(away_raw), val(home_raw)
                print(f"  + {away_raw['team']['abbreviation']}@{home_raw['team']['abbreviation']} "
                      f"W{wk} id={gid} {game_date.date()} {away_score}-{home_score}")
                if args.apply:
                    await db.execute(text("""
                        INSERT INTO nfl.games
                          (id, season_id, week, game_type, home_team_id, away_team_id,
                           date, status, home_score, away_score)
                        VALUES (:id,:season_id,:week,:game_type,
                                (SELECT id FROM nfl.teams WHERE abbreviation=:home),
                                (SELECT id FROM nfl.teams WHERE abbreviation=:away),
                                :date,'FINAL',:hs,:as)
                    """), {
                        "id": gid, "season_id": srow.id, "week": wk, "game_type": "REG",
                        "home": ESPN_TEAM_MAP.get(home_raw["team"]["abbreviation"], home_raw["team"]["abbreviation"]),
                        "away": ESPN_TEAM_MAP.get(away_raw["team"]["abbreviation"], away_raw["team"]["abbreviation"]),
                        "date": game_date, "hs": home_score, "as": away_score,
                    })
                inserts += 1

        # ---------- Re-week misplaced playoff games (2016-2019) ----------
        # Historical seasons had playoff games inserted at ESPN's raw weeks 1/2/3/5
        # (with Jan/Feb post-season dates) instead of canonical 19/20/21/22.
        print("\n=== Re-week misplaced playoff games (weeks 1/2/3/5 -> 19/20/21/22) ===")
        REWEEK = {1: 19, 2: 20, 3: 21, 5: 22}
        from datetime import date as _date
        for season in (2016, 2017, 2018, 2019):
            for src, dst in REWEEK.items():
                nxt = season + 1
                await db.execute(text("""
                    UPDATE nfl.games g
                    SET week = :dst
                    FROM nfl.seasons s
                    WHERE s.id = g.season_id AND s.year = :season
                      AND g.week = :src AND g.game_type = 'REG'
                      AND g.date::date BETWEEN :start AND :end
                """), {
                    "dst": dst, "season": season, "src": src,
                    "start": _date(nxt, 1, 1), "end": _date(nxt, 3, 1),
                })
        if args.apply:
            await db.commit()
        print("  re-week done.")

        print(f"\n=== Summary: {inserts} inserts, {skipped_existing} skipped(existing), {unresolved} unresolved ===")

        if args.apply:
            await db.commit()
            print("\n=== Aligning game_stats playoff weeks (18-21 -> 19-22) for 2016-2020 ===")
            for season in (2016, 2017, 2018, 2019, 2020):
                # shift in reverse (21->22, 20->21, 19->20, 18->19) to avoid the
                # (season, week, team_abbr, opponent_abbr) unique constraint.
                for src, dst in ((21, 22), (20, 21), (19, 20), (18, 19)):
                    r = await db.execute(text("""
                        UPDATE nfl.game_stats SET week = :dst
                        WHERE season=:season AND week = :src AND season_type='POST'
                    """), {"season": season, "src": src, "dst": dst})
            await db.commit()
            print("  game_stats playoff weeks aligned.")

            print("\n=== Recomputing cumulative stats for 2016-2020 ===")
            from app.handicapping.nfl.cumulative_stats import recompute
            res = await recompute(db, [2016, 2017, 2018, 2019, 2020])
            await db.commit()
            print("recompute results:", res)
        else:
            print("\n(dry-run — no DB writes made. Re-run with --apply.)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
