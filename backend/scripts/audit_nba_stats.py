"""
NBA stats audit — verify cumulative + rolling stats against raw nba.games boxscores.

For a sample of teams/seasons, recompute the stored feature values from the raw
boxscore columns in nba.games and compare to what's in nba.cumulative_game_stats
and nba.team_rolling_stats. Reports per-feature match/mismatch with max abs diff.

Possessions estimation (matches populate_team_rolling_stats):
    oreb_est = reb * 0.245 ; opp_oreb_est = opp_reb * 0.245
    poss = fga - oreb_est + 0.44*fta ; opp_poss = opp_fga - opp_oreb_est + 0.44*opp_fta
    ortg = 100*pts/poss ; drtg = 100*pts_allowed/opp_poss ; pace = (poss+opp_poss)/2
"""
import sys
from collections import defaultdict
import numpy as np
from sqlalchemy import create_engine, text

ENGINE = create_engine('postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football')

# Which team-seasons to audit (mix of playoff + non-playoff + recent)
SAMPLE = [
    (24, 2024),  # OKC (deep playoff)
    (9, 2024),   # HOU
    (15, 2023),  # sample
    (2, 2024),   # 
    (30, 2024),
]

PREFIX = {
    'cum_points': 'home_score', 'cum_points_allowed': 'away_score',
}

def get_side_boxes(c, season_year, team_id):
    """Return list of (date, side, pts, opp_pts, fgm,fga,fgm3,fga3,ftm,fta,reb,ast,stl,blk,tov,pf,
    opp_fgm,opp_fga,opp_fgm3,opp_fga3,opp_ftm,opp_fta,opp_reb,opp_ast,opp_stl,opp_blk,opp_tov,opp_pf)
    for REGULAR SEASON games only, newest first."""
    cols = [
        'g.date', 'g.home_team_id','g.away_team_id',
        'g.home_score','g.away_score',
        'g.home_field_goals_made','g.home_field_goals_attempted',
        'g.home_three_points_made','g.home_three_points_attempted',
        'g.home_free_throws_made','g.home_free_throws_attempted',
        'g.home_rebounds','g.home_assists','g.home_steals','g.home_blocks','g.home_turnovers','g.home_fouls',
        'g.away_field_goals_made','g.away_field_goals_attempted',
        'g.away_three_points_made','g.away_three_points_attempted',
        'g.away_free_throws_made','g.away_free_throws_attempted',
        'g.away_rebounds','g.away_assists','g.away_steals','g.away_blocks','g.away_turnovers','g.away_fouls',
    ]
    q = f"""
        SELECT {', '.join(cols)}
        FROM nba.games g
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE s.year = :yr AND (g.home_team_id = :t OR g.away_team_id = :t)
          AND g.game_type = 'REG' AND g.status = 'FINAL'
        ORDER BY g.date ASC, g.id ASC
    """
    games = []
    for r in c.execute(text(q), {'yr': season_year, 't': team_id}):
        d = dict(zip([f'c{i}' for i in range(len(r))], r))
        home = r[1] == team_id
        pts = r[3] if home else r[4]
        opp = r[4] if home else r[3]
        HB, AB = 5, 17  # home stats start at idx5, away at idx17
        def s(k):
            return r[HB+k] if home else r[AB+k]
        def sopp(k):
            return r[AB+k] if home else r[HB+k]
        def clean(v):
            return 0 if v is None else v
        g = {
            'date': r[0], 'side': 'home' if home else 'away',
            'pts': clean(pts), 'opp': clean(opp),
            'fgm': clean(s(0)),'fga': clean(s(1)),'fgm3': clean(s(2)),'fga3': clean(s(3)),
            'ftm': clean(s(4)),'fta': clean(s(5)),'reb': clean(s(6)),'ast': clean(s(7)),
            'stl': clean(s(8)),'blk': clean(s(9)),'tov': clean(s(10)),'pf': clean(s(11)),
            'opp_fgm': clean(sopp(0)),'opp_fga': clean(sopp(1)),'opp_fgm3': clean(sopp(2)),'opp_fga3': clean(sopp(3)),
            'opp_ftm': clean(sopp(4)),'opp_fta': clean(sopp(5)),'opp_reb': clean(sopp(6)),
            'opp_ast': clean(sopp(7)),'opp_stl': clean(sopp(8)),'opp_blk': clean(sopp(9)),'opp_tov': clean(sopp(10)),'opp_pf': clean(sopp(11)),
        }
        g['null_box_fields'] = sum(1 for v in r[5:29] if v is None)
        games.append(g)
    return games


def poss_from(g):
    oreb = g['reb']*0.245
    ooreb = g['opp_reb']*0.245
    p = g['fga'] - oreb + 0.44*g['fta']
    op = g['opp_fga'] - ooreb + 0.44*g['opp_fta']
    return p, op


def main():
    with ENGINE.connect() as c:
        print("=" * 78)
        print("NBA CUMULATIVE + ROLLING STAT AUDIT (vs raw nba.games boxscores)")
        print("=" * 78)
        for (team_id, yr) in SAMPLE:
            games = get_side_boxes(c, yr, team_id)
            if not games:
                print(f"\n[{team_id} {yr}] NO GAMES"); continue
            n = len(games)
            # cumulative recompute (post-game incl. all REG games)
            cum = {k: sum(g[k] for g in games) for k in
                   ['pts','opp','fgm','fga','fgm3','fga3','ftm','fta','reb','ast','stl','blk','tov','pf',
                    'opp_fgm','opp_fga','opp_fgm3','opp_fga3','opp_ftm','opp_fta','opp_reb','opp_ast','opp_stl','opp_blk','opp_tov','opp_pf']}
            efg = (cum['fgm'] + 0.5*cum['fgm3'])/cum['fga'] if cum['fga'] else None
            told = cum['tov']/cum['opp_fgm'] if cum['opp_fgm'] else None
            # stored final cumulative row (fetch from table, last by game_id)
            srow = c.execute(text('''
                SELECT cum_ppg, cum_oppg, cum_margin_pg, cum_fg_pct, cum_efg_pct, cum_ast_ratio, cum_tov_rate, cum_ft_rate,
                       cum_ortg, cum_drtg, cum_pace, cum_win_pct, games_played
                FROM nba.cumulative_game_stats
                WHERE team_id=:t AND season_id=(SELECT id FROM nba.seasons WHERE year=:yr)
                  AND game_id=(SELECT max(game_id) FROM nba.cumulative_game_stats WHERE team_id=:t
                               AND season_id=(SELECT id FROM nba.seasons WHERE year=:yr))
            '''), {'t': team_id, 'yr': yr}).fetchone()
            if srow is None:
                print(f"\n[{team_id} {yr}] no stored cumulative final row!"); continue
            exp = {
                'cum_ppg': cum['pts']/n,
                'cum_oppg': cum['opp']/n,
                'cum_fg_pct': cum['fgm']/cum['fga'] if cum['fga'] else None,
                'cum_efg_pct': efg,
                'cum_ast_ratio': cum['ast']/cum['fgm'] if cum['fgm'] else None,
                'cum_tov_rate': cum['tov']/cum['opp_fgm'] if cum['opp_fgm'] else None,
                'cum_ft_rate': cum['fta']/cum['fga'] if cum['fga'] else None,
            }
            print(f"\n### TEAM {team_id} / season {yr} : {n} REG games (stored final gp={srow[12]})")
            nullg = [g for g in games if g.get('null_box_fields',0)]
            if nullg:
                totnull = sum(g['null_box_fields'] for g in nullg)
                print(f"  ⚠️  {len(nullg)}/{n} games have NULL boxscore fields ({totnull} total NULL cols)")
            else:
                print(f"  boxscores: all {n} games fully populated")
            for i, k in enumerate(['cum_ppg','cum_oppg','cum_fg_pct','cum_efg_pct','cum_ast_ratio','cum_tov_rate','cum_ft_rate']):
                got = float(srow[i]) if srow[i] is not None else None
                want = exp[k]
                if want is not None:
                    diff = abs(got-want) if got is not None else 999
                    flag = "OK " if diff < 0.001 else ("**MISMATCH**" )
                    print(f"  {k:16s} stored={got if got is not None else 'NULL':>10}  recomputed={want:>10.6f}  {flag}")

            # --- ROLLING recompute for the last REG game (r5/r10 net_rtg, ortg, drtg, pace, efg) ---
            last = games[-1]
            n5 = games[-5:] if n >= 5 else games
            n10 = games[-10:] if n >= 10 else games
            def rolling_avg(gs, fn, name):
                vals = [fn(g) for g in gs]
                vals = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))]
                return np.mean(vals) if vals else None
            def netrtg(g):
                p, op = poss_from(g); 
                try: return 100*g['pts']/p - 100*g['opp']/op
                except ZeroDivisionError: return None
            def ortg(g):
                p, _ = poss_from(g)
                return 100*g['pts']/p if p else None
            def drtg(g):
                _, op = poss_from(g)
                return 100*g['opp']/op if op else None
            def pace(g):
                p, op = poss_from(g); return (p+op)/2
            def efg(g): return (g['fgm']+0.5*g['fgm3'])/g['fga'] if g['fga'] else None

            # stored rolling last REG row
            from sqlalchemy import text as _t
            srow2 = c.execute(_t('''
                SELECT game_id, net_rtg_r5, net_rtg_r10, ortg_r5, drtg_r10, pace_r5, efg_r10
                FROM nba.team_rolling_stats
                WHERE team_id=:t AND season_id=(SELECT id FROM nba.seasons WHERE year=:yr)
                  AND game_id=(SELECT max(game_id) FROM nba.team_rolling_stats
                               WHERE team_id=:t AND season_id=(SELECT id FROM nba.seasons WHERE year=:yr)
                                 AND game_id IN (SELECT c.game_id FROM nba.cumulative_game_stats c
                                                JOIN nba.games g ON g.id=c.game_id
                                                WHERE c.team_id=:t AND g.game_type='REG'
                                                  AND c.season_id=(SELECT id FROM nba.seasons WHERE year=:yr)))
            '''), {'t': team_id, 'yr': yr}).fetchone()
            if srow2 is None:
                print("  rolling: no stored last-REG row")
            else:
                checks = [
                    ('net_rtg_r5', srow2[1], rolling_avg(n5, netrtg, 'netrtg')),
                    ('net_rtg_r10', srow2[2], rolling_avg(n10, netrtg, 'netrtg')),
                    ('ortg_r5', srow2[3], rolling_avg(n5, ortg, 'ortg')),
                    ('drtg_r10', srow2[4], rolling_avg(n10, drtg, 'drtg')),
                    ('pace_r5', srow2[5], rolling_avg(n5, pace, 'pace')),
                    ('efg_r10', srow2[6], rolling_avg(n10, efg, 'efg')),
                ]
                print("  [rolling, last REG game] stored vs recomputed (inclusive of that game):")
                for name, got, want in checks:
                    diff = abs(got-want) if (got is not None and want is not None) else 999
                    flag = "OK " if diff < 0.11 else ("**MISMATCH**" if diff < 999 else "NULL?")
                    print(f"    {name:14s} stored={got if got is not None else 'NULL':>8}  recomputed={want if want is not None else 'NULL':>8.4f}  {flag}")

if __name__ == '__main__':
    main()
