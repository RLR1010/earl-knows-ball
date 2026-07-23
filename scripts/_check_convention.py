#!/usr/bin/env python3
"""Check spread convention: negative spread = home favored?"""
import psycopg2, psycopg2.extras
conn = psycopg2.connect(host='localhost', port=5432, dbname='earl_knows_football', user='earl', password='NVh1g…L0')
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=== NFL betting_lines_old spread convention ===")
cur.execute("""
    SELECT blo.game_id, blo.spread as closing_spread, blo.home_moneyline, blo.away_moneyline,
           g.date, home.name as home_team, away.name as away_team
    FROM nfl.betting_lines_old blo
    JOIN nfl.games g ON g.id = blo.game_id
    JOIN nfl.teams home ON home.id = g.home_team_id
    JOIN nfl.teams away ON away.id = g.away_team_id
    WHERE blo.spread IS NOT NULL AND blo.home_moneyline IS NOT NULL
    ORDER BY g.date
    LIMIT 15
""")
for r in cur.fetchall():
    home_favored = r['home_moneyline'] < 0
    spread_says_home_fav = r['closing_spread'] < 0
    match = "✅" if home_favored == spread_says_home_fav else "❌"
    print(f"  {match} {r['home_team']} vs {r['away_team']} | spread={r['closing_spread']} | "
          f"home_ml={r['home_moneyline']} (favored={'yes' if home_favored else 'no'})")

print()
print("=== MLB betting_lines_old spread convention ===")
cur.execute("""
    SELECT blo.game_id, blo.spread as opening_spread, blo.home_moneyline, blo.away_moneyline,
           g.date, home.name as home_team, away.name as away_team
    FROM mlb.betting_lines_old blo
    JOIN mlb.games g ON g.id = blo.game_id
    JOIN mlb.teams home ON home.id = g.home_team_id
    JOIN mlb.teams away ON away.id = g.away_team_id
    WHERE blo.spread IS NOT NULL AND blo.home_moneyline IS NOT NULL
    ORDER BY g.date
    LIMIT 15
""")
for r in cur.fetchall():
    home_favored = r['home_moneyline'] < 0
    spread_says_home_fav = r['opening_spread'] < 0
    match = "✅" if home_favored == spread_says_home_fav else "❌"
    print(f"  {match} {r['home_team']} vs {r['away_team']} | spread={r['opening_spread']} | "
          f"home_ml={r['home_moneyline']} (favored={'yes' if home_favored else 'no'})")

print()
print("=== MLB betting_lines_old spread_odds convention ===")
cur.execute("""
    SELECT game_id, spread, spread_home_odds, spread_away_odds, home_moneyline, away_moneyline
    FROM mlb.betting_lines_old
    WHERE spread_home_odds IS NOT NULL
    ORDER BY game_id
    LIMIT 10
""")
for r in cur.fetchall():
    print(f"  spread={r['spread']}, home_odds={r['spread_home_odds']}, away_odds={r['spread_away_odds']}, "
          f"home_ml={r['home_moneyline']}, away_ml={r['away_moneyline']}")

print()
print("=== Check existing NFL consolidated for comparison ===")
cur.execute("""
    SELECT closing_spread, closing_home_ml, home_team, away_team
    FROM nfl.betting_lines_consolidated
    WHERE closing_spread IS NOT NULL
    LIMIT 10
""")
for r in cur.fetchall():
    home_favored = r['closing_home_ml'] < 0
    spread_says_home_fav = r['closing_spread'] < 0
    match = "✅" if home_favored == spread_says_home_fav else "❌"
    print(f"  {match} {r['home_team']} vs {r['away_team']} | spread={r['closing_spread']} | home_ml={r['closing_home_ml']}")

print()
print("=== Check existing MLB consolidated for comparison ===")
cur.execute("""
    SELECT opening_spread, opening_home_ml, home_team, away_team
    FROM mlb.betting_lines_consolidated
    WHERE opening_spread IS NOT NULL
    LIMIT 10
""")
for r in cur.fetchall():
    home_favored = r['opening_home_ml'] < 0
    spread_says_home_fav = r['opening_spread'] < 0
    match = "✅" if home_favored == spread_says_home_fav else "❌"
    print(f"  {match} {r['home_team']} vs {r['away_team']} | spread={r['opening_spread']} | home_ml={r['opening_home_ml']}")

conn.close()
