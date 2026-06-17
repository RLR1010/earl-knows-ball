"""
Rebuild nfl.game_lines from betting_lines sources.

Consolidates per-sportsbook data into a single row per game with
opening and closing lines, spreads, odds, and moneylines.

Priority chain:
  Closing: the_odds_api_closing (2021+) > nflverse (2006+) > fallback
  Opening: the_odds_api_opening (2021+) > sbr_opening (2011-2020) > nflverse_fallback

Usage: python -m app.ingestion.rebuild_game_lines
"""
import asyncio, logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger('earl.rebuild_game_lines')

DB_URL = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"

# Default juice when odds are missing
DEFAULT_SPREAD_ODDS = -110
DEFAULT_OU_ODDS = -110

# Spread → moneyline conversion (derived from 2021-2025 The Odds API data)
# For spreads not in this table, linear interpolation is used
SPREAD_ML_MAP = [
    (0, -107), (1, -119), (2, -134), (3, -160), (4, -197),
    (5, -234), (6, -264), (7, -322), (8, -382), (9, -442),
    (10, -493), (11, -567), (12, -737), (13, -815), (14, -1029),
]


def spread_to_ml(spread: float, is_home: bool) -> int | None:
    """
    Estimate moneyline from opening spread.
    Uses interpolation from actual 2021-2025 data.
    For home (favored): returns negative ML (e.g., -160)
    For away (dog): returns positive ML (e.g., +135)
    """
    if spread is None or spread <= 0:
        # Pick'em or home underdog — can't reliably estimate
        return None
    
    spread = round(spread, 1)
    
    # Linear interpolation from lookup table
    home_ml = None
    for i in range(len(SPREAD_ML_MAP) - 1):
        s_low, ml_low = SPREAD_ML_MAP[i]
        s_high, ml_high = SPREAD_ML_MAP[i + 1]
        if s_low <= spread <= s_high:
            ratio = (spread - s_low) / (s_high - s_low)
            home_ml = int(ml_low + ratio * (ml_high - ml_low))
            break
    
    if home_ml is None:
        if spread >= SPREAD_ML_MAP[-1][0]:
            home_ml = int(SPREAD_ML_MAP[-1][1] * (spread / SPREAD_ML_MAP[-1][0]))
        else:
            home_ml = SPREAD_ML_MAP[0][1]
    
    if is_home:
        return home_ml
    else:
        # Derive away ML from home ML using no-vig + vig split
        # Home implied = |ML| / (|ML| + 100)
        home_implied = abs(home_ml) / (abs(home_ml) + 100)
        # Total implied with ~4% vig
        away_implied = (1.04 - home_implied) if home_implied < 0.96 else 0.05
        # Convert back to American odds (positive for dog)
        if away_implied > 0.5:
            # Shouldn't happen for a dog, but handle it
            return int(-100 * away_implied / (1 - away_implied))
        else:
            return int(round(100 * (1 - away_implied) / away_implied))


def avg_int(vals: list) -> int | None:
    """Average a list of ints, rounding to nearest int."""
    if not vals:
        return None
    return int(round(sum(vals) / len(vals)))


async def rebuild():
    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # Get all games with season info
        r = await db.execute(sql_text("""
            SELECT g.id, g.date, s.year, g.week
            FROM nfl.games g
            JOIN nfl.seasons s ON s.id = g.season_id
            ORDER BY s.year, g.date
        """))
        all_games = {g.id: g for g in r.fetchall()}
        logger.info(f"Total NFL games: {len(all_games)}")

        # ── Closing lines ─────────────────────────────────────────────
        # Priority: the_odds_api_closing > nflverse
        r = await db.execute(sql_text("""
            SELECT game_id,
                   AVG(spread) as spread,
                   AVG(over_under) as over_under,
                   AVG(spread_home_odds) as spread_home_odds,
                   AVG(spread_away_odds) as spread_away_odds,
                   AVG(over_odds) as over_odds,
                   AVG(under_odds) as under_odds,
                   AVG(home_moneyline) as home_moneyline,
                   AVG(away_moneyline) as away_moneyline
            FROM nfl.betting_lines
            WHERE source = 'the_odds_api_closing'
              AND game_id = ANY(:gids)
              AND spread_home_odds > -1000
              AND spread_away_odds > -1000
              AND over_odds > -1000
              AND under_odds > -1000
            GROUP BY game_id
        """), {"gids": list(all_games.keys())})
        closing_api = {r.game_id: r for r in r.fetchall()}

        r = await db.execute(sql_text("""
            SELECT game_id,
                   spread, over_under,
                   home_moneyline, away_moneyline,
                   spread_home_odds, spread_away_odds,
                   over_odds, under_odds
            FROM nfl.betting_lines
            WHERE source = 'nflverse'
              AND game_id = ANY(:gids)
        """), {"gids": list(all_games.keys())})
        closing_nflverse = {r.game_id: r for r in r.fetchall()}

        # ── Opening lines ─────────────────────────────────────────────
        # Priority: the_odds_api_opening > sbr_opening > nflverse_fallback

        # Opening from The Odds API (2021+)
        r = await db.execute(sql_text("""
            SELECT game_id,
                   AVG(spread) as spread,
                   AVG(over_under) as over_under,
                   AVG(spread_home_odds) as spread_home_odds,
                   AVG(spread_away_odds) as spread_away_odds,
                   AVG(over_odds) as over_odds,
                   AVG(under_odds) as under_odds,
                   AVG(home_moneyline) as home_moneyline,
                   AVG(away_moneyline) as away_moneyline
            FROM nfl.betting_lines
            WHERE source = 'the_odds_api_opening'
              AND game_id = ANY(:gids)
              AND spread_home_odds > -1000  -- filter sentinel values
              AND spread_away_odds > -1000
              AND over_odds > -1000
              AND under_odds > -1000
            GROUP BY game_id
        """), {"gids": list(all_games.keys())})
        opening_api = {r.game_id: r for r in r.fetchall()}

        # Opening from SBR (2011-2020)
        r = await db.execute(sql_text("""
            SELECT game_id,
                   spread, over_under,
                   home_moneyline, away_moneyline
            FROM nfl.betting_lines
            WHERE source = 'sbr_opening'
              AND game_id = ANY(:gids)
        """), {"gids": list(all_games.keys())})
        opening_sbr = {r.game_id: r for r in r.fetchall()}

        # ── Per-sportsbook opening odds (for the_odds_api_opening) ────
        r = await db.execute(sql_text("""
            SELECT game_id, 
                   spread_home_odds, spread_away_odds,
                   over_odds, under_odds,
                   home_moneyline, away_moneyline
            FROM nfl.betting_lines
            WHERE source = 'the_odds_api_opening'
              AND game_id = ANY(:gids)
              AND spread_home_odds > -1000
        """), {"gids": list(all_games.keys())})
        opening_api_odds = list(r.fetchall())

        # Build per-game opening odds aggregates
        opening_odds = {}
        for row in opening_api_odds:
            gid = row.game_id
            if gid not in opening_odds:
                opening_odds[gid] = {'sp_h': [], 'sp_a': [], 'o': [], 'u': [], 'hml': [], 'aml': []}
            if row.spread_home_odds is not None:
                opening_odds[gid]['sp_h'].append(row.spread_home_odds)
            if row.spread_away_odds is not None:
                opening_odds[gid]['sp_a'].append(row.spread_away_odds)
            if row.over_odds is not None:
                opening_odds[gid]['o'].append(row.over_odds)
            if row.under_odds is not None:
                opening_odds[gid]['u'].append(row.under_odds)
            if row.home_moneyline is not None:
                opening_odds[gid]['hml'].append(row.home_moneyline)
            if row.away_moneyline is not None:
                opening_odds[gid]['aml'].append(row.away_moneyline)

        # ── Build game_lines rows ──────────────────────────────────────
        rows = []
        stats = {"api_closing": 0, "nflverse_closing": 0, "no_closing": 0,
                 "api_opening": 0, "sbr_opening": 0, "nflverse_opening": 0, "no_opening": 0}

        for gid, game in all_games.items():
            year = game.year
            row = {"game_id": gid}

            # ── Closing line ──
            cl = None
            src_close = None
            if gid in closing_api and closing_api[gid].spread is not None:
                cl = closing_api[gid]
                src_close = "the_odds_api_closing"
                stats["api_closing"] += 1
            elif gid in closing_nflverse and closing_nflverse[gid].spread is not None:
                cl = closing_nflverse[gid]
                src_close = "nflverse"
                stats["nflverse_closing"] += 1

            if cl:
                row["spread"] = cl.spread
                row["over_under"] = cl.over_under
                row["home_moneyline"] = cl.home_moneyline
                row["away_moneyline"] = cl.away_moneyline
                row["source_closing"] = src_close

                # Closing odds: prefer API (per-sportsbook), fall back to nflverse, default -110
                if gid in closing_api:
                    api_cl = closing_api[gid]
                    row["spread_home_odds"] = api_cl.spread_home_odds or DEFAULT_SPREAD_ODDS
                    row["spread_away_odds"] = api_cl.spread_away_odds or DEFAULT_SPREAD_ODDS
                    row["over_odds"] = api_cl.over_odds or DEFAULT_OU_ODDS
                    row["under_odds"] = api_cl.under_odds or DEFAULT_OU_ODDS
                elif getattr(cl, 'spread_home_odds', None) is not None:
                    row["spread_home_odds"] = cl.spread_home_odds
                    row["spread_away_odds"] = cl.spread_away_odds
                    row["over_odds"] = cl.over_odds
                    row["under_odds"] = cl.under_odds
                else:
                    row["spread_home_odds"] = DEFAULT_SPREAD_ODDS
                    row["spread_away_odds"] = DEFAULT_SPREAD_ODDS
                    row["over_odds"] = DEFAULT_OU_ODDS
                    row["under_odds"] = DEFAULT_OU_ODDS
            else:
                stats["no_closing"] += 1
                continue  # skip games with no closing line at all

            # ── Opening line ──
            ol = None
            src_open = None

            if gid in opening_api:
                ol = opening_api[gid]
                src_open = "the_odds_api_opening"
                stats["api_opening"] += 1
            elif gid in opening_sbr:
                ol = opening_sbr[gid]
                src_open = "sbr_opening"
                stats["sbr_opening"] += 1
            elif gid in closing_nflverse:
                # Fallback: use closing line as opening line proxy
                nv = closing_nflverse[gid]
                if nv.spread is not None and nv.over_under is not None:
                    ol = nv
                    src_open = "nflverse_fallback"
                    stats["nflverse_opening"] += 1

            if ol:
                row["opening_spread"] = ol.spread
                row["opening_ou"] = ol.over_under
                row["source_opening"] = src_open

                # Opening odds
                if src_open == "the_odds_api_opening" and gid in opening_odds:
                    oo = opening_odds[gid]
                    row["opening_spread_home_odds"] = avg_int(oo['sp_h'])
                    row["opening_spread_away_odds"] = avg_int(oo['sp_a'])
                    row["opening_over_odds"] = avg_int(oo['o'])
                    row["opening_under_odds"] = avg_int(oo['u'])
                    row["opening_home_moneyline"] = avg_int(oo['hml'])
                    row["opening_away_moneyline"] = avg_int(oo['aml'])
                else:
                    # Default odds for SBR and fallback
                    row["opening_spread_home_odds"] = DEFAULT_SPREAD_ODDS
                    row["opening_spread_away_odds"] = DEFAULT_SPREAD_ODDS
                    row["opening_over_odds"] = DEFAULT_OU_ODDS
                    row["opening_under_odds"] = DEFAULT_OU_ODDS
                    # Infer opening ML from opening spread
                    osp = row.get("opening_spread")
                    if osp is not None and osp > 0:
                        row["opening_home_moneyline"] = spread_to_ml(osp, is_home=True)
                        row["opening_away_moneyline"] = spread_to_ml(osp, is_home=False)
                    elif osp is not None and osp < 0:
                        # Home is underdog, away is favored
                        row["opening_home_moneyline"] = spread_to_ml(abs(osp), is_home=False)
                        row["opening_away_moneyline"] = spread_to_ml(abs(osp), is_home=True)
                    else:
                        row["opening_home_moneyline"] = None
                        row["opening_away_moneyline"] = None
            else:
                stats["no_opening"] += 1
                continue  # skip game with no opening data

            rows.append(row)

        # ── Clear and insert ──
        logger.info(f"Rebuilding game_lines: {len(rows)} games")
        logger.info(f"  Closing: the_odds_api_closing={stats['api_closing']}, "
                    f"nflverse={stats['nflverse_closing']}, none={stats['no_closing']}")
        logger.info(f"  Opening: api={stats['api_opening']}, sbr={stats['sbr_opening']}, "
                    f"nflverse_fb={stats['nflverse_opening']}, none={stats['no_opening']}")

        await db.execute(sql_text("TRUNCATE nfl.game_lines"))

        if rows:
            await db.execute(sql_text("""
                INSERT INTO nfl.game_lines
                (game_id, spread, over_under, home_moneyline, away_moneyline,
                 spread_home_odds, spread_away_odds, over_odds, under_odds,
                 opening_spread, opening_ou,
                 opening_spread_home_odds, opening_spread_away_odds,
                 opening_over_odds, opening_under_odds,
                 opening_home_moneyline, opening_away_moneyline,
                 source_opening, source_closing)
                VALUES (:game_id, :spread, :over_under, 
                        :home_moneyline, :away_moneyline,
                        :spread_home_odds, :spread_away_odds,
                        :over_odds, :under_odds,
                        :opening_spread, :opening_ou,
                        :opening_spread_home_odds, :opening_spread_away_odds,
                        :opening_over_odds, :opening_under_odds,
                        :opening_home_moneyline, :opening_away_moneyline,
                        :source_opening, :source_closing)
            """), rows)
            await db.commit()

        # Verify
        r = await db.execute(sql_text("SELECT COUNT(*) FROM nfl.game_lines"))
        total = r.scalar()
        logger.info(f"Done. {total} rows in game_lines")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(rebuild())
