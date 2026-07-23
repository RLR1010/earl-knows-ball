#!/usr/bin/env python3
"""Audit all three betting_lines_consolidated tables."""
import psycopg2, psycopg2.extras
conn = psycopg2.connect(host='localhost', port=5432, dbname='earl_knows_football', user='earl', password=***
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

for schema in ('nfl', 'nba', 'mlb'):
    print(f"\n{'='*60}")
    print(f"  {schema.upper()} betting_lines_consolidated")
    print(f"{'='*60}")

    cur.execute(f"SELECT COUNT(*) FROM {schema}.betting_lines_consolidated")
    total = cur.fetchone()['count']
    print(f"  Total rows: {total}")

    cur.execute(f"""
        SELECT year, COUNT(*) as rows, COUNT(DISTINCT game_id) as games,
               COUNT(closing_spread) as has_close_spread,
               COUNT(closing_home_ml) as has_close_ml,
               COUNT(closing_over_odds) as has_close_over_odds,
               COUNT(opening_spread) as has_open_spread,
               COUNT(opening_home_ml) as has_open_ml,
               COUNT(opening_over_odds) as has_open_over_odds,
               COUNT(closing_home_implied_probability) as has_close_ip,
               COUNT(opening_home_implied_probability) as has_open_ip
        FROM {schema}.betting_lines_consolidated
        GROUP BY year ORDER BY year
    """)
    rows = cur.fetchall()
    if rows:
        print(f"  {'Year':<6} {'Rows':<6} {'Games':<6} {'ClsSprd':<8} {'ClsML':<6} {'ClsOvOd':<8} {'OpnSprd':<8} {'OpnML':<6} {'OpnOvOd':<8} {'ClsIP':<6} {'OpnIP':<6}")
        print(f"  {'-'*72}")
        for r in rows:
            print(f"  {r['year']:<6} {r['rows']:<6} {r['games']:<6} "
                  f"{r['has_close_spread']:<8} {r['has_close_ml']:<6} {r['has_close_over_odds']:<8} "
                  f"{r['has_open_spread']:<8} {r['has_open_ml']:<6} {r['has_open_over_odds']:<8} "
                  f"{r['has_close_ip']:<6} {r['has_open_ip']:<6}")

    # Sources
    cur.execute(f"""
        SELECT DISTINCT closing_spread_sportsbook
        FROM {schema}.betting_lines_consolidated
        WHERE closing_spread IS NOT NULL
        ORDER BY closing_spread_sportsbook
    """)
    sources = [r['closing_spread_sportsbook'] for r in cur.fetchall() if r['closing_spread_sportsbook']]
    print(f"\n  Sources: {', '.join(sources) if sources else 'NONE'}")

    # Bad IPs
    cur.execute(f"""
        SELECT COUNT(*) FROM {schema}.betting_lines_consolidated
        WHERE closing_home_implied_probability > 100
           OR opening_home_implied_probability > 100
    """)
    bad_ip = cur.fetchone()['count']
    if bad_ip:
        print(f"  [BAD] {bad_ip} rows still have bad implied probabilities (>100)")
    else:
        print(f"  [OK] All implied probabilities correct")

    # Spread/ML convention
    cur.execute(f"""
        SELECT COUNT(*) FROM {schema}.betting_lines_consolidated
        WHERE closing_spread IS NOT NULL AND closing_home_ml IS NOT NULL
          AND ((closing_spread < 0 AND closing_home_ml > 0)
               OR (closing_spread > 0 AND closing_home_ml < 0))
    """)
    mismatches = cur.fetchone()['count']
    if mismatches:
        cur.execute(f"""
            SELECT home_team, away_team, closing_spread, closing_home_ml, year
            FROM {schema}.betting_lines_consolidated
            WHERE closing_spread IS NOT NULL AND closing_home_ml IS NOT NULL
              AND ((closing_spread < 0 AND closing_home_ml > 0)
                   OR (closing_spread > 0 AND closing_home_ml < 0))
            LIMIT 5
        """)
        print(f"  [BAD] {mismatches} spread/ML convention mismatches:")
        for r in cur.fetchall():
            print(f"      {r['home_team']} vs {r['away_team']} ({r['year']}): spread={r['closing_spread']}, ml={r['closing_home_ml']}")
    else:
        print(f"  [OK] Spread/ML convention consistent")

    # Null gaps
    cur.execute(f"""
        SELECT COUNT(*) FROM {schema}.betting_lines_consolidated
        WHERE closing_spread IS NULL AND closing_home_ml IS NULL
    """)
    both_null = cur.fetchone()['count']
    if both_null:
        print(f"  [WARN] {both_null} rows with closing_spread AND closing_home_ml both null")

    cur.execute(f"""
        SELECT COUNT(*) FROM {schema}.betting_lines_consolidated
        WHERE closing_spread IS NOT NULL AND closing_spread_home_odds IS NULL
          AND closing_spread_sportsbook IS NOT NULL
    """)
    spread_no_odds = cur.fetchone()['count']
    if spread_no_odds:
        print(f"  [WARN] {spread_no_odds} rows have closing spread but no spread odds")

conn.close()
