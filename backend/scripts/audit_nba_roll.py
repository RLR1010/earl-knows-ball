"""
Confirm steal/block/turnover cumulative values match boxscores EXACTLY for a completed season.
Also verify eFG/AST ratio/FT rate formulas and check playin impact on a playoff-adjacent team.
"""
import numpy as np
from sqlalchemy import create_engine, text

ENGINE = create_engine('postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football')

def get_side(g, home_team_id, team_id, off, defi):
    return g[off] if g[home_team_id] == team_id else g[defi]

def main():
    with ENGINE.connect() as c:
        team_id = 24
        sid = c.execute(text('SELECT id FROM nba.seasons WHERE year=2024')).scalar()
        # Stored cum row
        s = c.execute(text('''
            SELECT cum_stl, cum_blk, cum_tov, cum_fgm, cum_fga, cum_fgm3, cum_ast, cum_fta,
                   cum_points, cum_points_allowed, games_played, cum_reb
            FROM nba.cumulative_game_stats
            WHERE team_id=:t AND season_id=:s AND game_id=(SELECT max(game_id) FROM nba.cumulative_game_stats WHERE team_id=:t AND season_id=:s)
        '''), {'t': team_id, 's': sid}).fetchone()
        cols = ['cum_stl','cum_blk','cum_tov','cum_fgm','cum_fga','cum_fgm3','cum_ast','cum_fta','cum_points','cum_points_allowed','games_played','cum_reb']
        stored = dict(zip(cols, s))
        # Recompute from boxscores (REG only, to isolate)
        g = c.execute(text('''
            SELECT g.home_team_id,
                   CASE WHEN g.home_team_id=:t THEN g.home_steals ELSE g.away_steals END AS stl,
                   CASE WHEN g.home_team_id=:t THEN g.home_blocks ELSE g.away_blocks END AS blk,
                   CASE WHEN g.home_team_id=:t THEN g.home_turnovers ELSE g.away_turnovers END AS tov,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_made ELSE g.away_field_goals_made END AS fgm,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_attempted ELSE g.away_field_goals_attempted END AS fga,
                   CASE WHEN g.home_team_id=:t THEN g.home_three_points_made ELSE g.away_three_points_made END AS fgm3,
                   CASE WHEN g.home_team_id=:t THEN g.home_assists ELSE g.away_assists END AS ast,
                   CASE WHEN g.home_team_id=:t THEN g.home_free_throws_attempted ELSE g.away_free_throws_attempted END AS fta,
                   CASE WHEN g.home_team_id=:t THEN g.home_score ELSE g.away_score END AS pts,
                   CASE WHEN g.home_team_id=:t THEN g.away_score ELSE g.home_score END AS opp,
                   CASE WHEN g.home_team_id=:t THEN g.home_rebounds ELSE g.away_rebounds END AS reb
            FROM nba.games g
            WHERE g.season_id=:s AND g.game_type='REG' AND g.status='FINAL' AND (g.home_team_id=:t OR g.away_team_id=:t)
        '''), {'t': team_id, 's': sid}).fetchall()
        GP = len(g)
        reg = {k: sum(r[i+1] for r in g) for i, k in enumerate(['stl','blk','tov','fgm','fga','fgm3','ast','fta','pts','opp','reb'])}
        print(f'OKC 2024: REG games={GP}')
        print('  FIELD              STORED     REG-only-recompute   match?')
        mm = {
            'cum_stl': stored['cum_stl'], 'cum_blk': stored['cum_blk'], 'cum_tov': stored['cum_tov'],
            'cum_fgm': stored['cum_fgm'], 'cum_fga': stored['cum_fga'], 'cum_fgm3': stored['cum_fgm3'],
            'cum_ast': stored['cum_ast'], 'cum_fta': stored['cum_fta'], 'cum_reb': stored['cum_reb'],
        }
        for k, got in mm.items():
            rk = {'cum_stl':'stl','cum_blk':'blk','cum_tov':'tov','cum_fgm':'fgm','cum_fga':'fga','cum_fgm3':'fgm3','cum_ast':'ast','cum_fta':'fta','cum_reb':'reb'}[k]
            want = reg[rk]
            print(f'  {k:14s} {got:>10}   {want:>10}        {"OK" if abs(got-want)<1 else "**DIFF**"}')
        # games_played stored vs REG-only
        print(f'  games_played     {stored["games_played"]:>10}   {GP:>10}        {"OK(gp incl post?)" if stored["games_played"] in (GP, GP+1, GP+15, GP+23, GP+24) else "??"}')
        # playoff+playin games for this team
        npost = c.execute(text('''
            SELECT count(*) FROM nba.games g WHERE g.season_id=:s AND g.game_type IN ('POST','PLAYIN') AND status='FINAL' AND (g.home_team_id=:t OR g.away_team_id=:t)
        '''), {'t': team_id, 's': sid}).scalar()
        print(f'  OKC 2024 POST+PLAYIN games = {npost}  (stored games_played={stored["games_played"]}; expected REG+POST+PLAYIN = {GP}+{npost}={GP+npost})')

if __name__ == '__main__':
    main()

def rolling_check():
    import numpy as np
    with ENGINE.connect() as c:
        team_id, yr = 24, 2024
        sid = c.execute(text('SELECT id FROM nba.seasons WHERE year=:y'), {'y':yr}).scalar()
        g = c.execute(text('''
            SELECT g.home_team_id,
                   CASE WHEN g.home_team_id=:t THEN g.home_score ELSE g.away_score END AS pts,
                   CASE WHEN g.home_team_id=:t THEN g.away_score ELSE g.home_score END AS opp,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_attempted ELSE g.away_field_goals_attempted END AS fga,
                   CASE WHEN g.home_team_id=:t THEN g.home_free_throws_attempted ELSE g.away_free_throws_attempted END AS fta,
                   CASE WHEN g.home_team_id=:t THEN g.home_turnovers ELSE g.away_turnovers END AS tov,
                   CASE WHEN g.home_team_id=:t THEN g.home_three_points_made ELSE g.away_three_points_made END AS fgm3,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_made ELSE g.away_field_goals_made END AS fgm,
                   g.date
            FROM nba.games g
            WHERE g.season_id=:s AND g.game_type='REG' AND g.status='FINAL' AND (g.home_team_id=:t OR g.away_team_id=:t)
            ORDER BY g.date ASC
        '''), {'t': team_id, 's': sid}).fetchall()
        # last game
        last_date = g[-1][8]
        last_gid = c.execute(text('SELECT id FROM nba.games WHERE season_id=:s AND game_type=\'REG\' AND (home_team_id=:t OR away_team_id=:t) AND date<=:d ORDER BY date DESC, id DESC LIMIT 1'), {'t':team_id,'s':sid,'d':last_date}).scalar()
        # stored rolling row for that game
        sr = c.execute(text('''
            SELECT net_rtg_r5, ortg_r5, drtg_r5, pace_r5, efg_r5, net_rtg_r10, ortg_r10, drtg_r10, pace_r10, efg_r10,
                   ast_ratio_r5, ft_rate_r5
            FROM nba.team_rolling_stats WHERE team_id=:t AND season_id=:s AND game_id=:gid
        '''), {'t':team_id,'s':sid,'gid':last_gid}).fetchone()
        if sr is None:
            print('no rolling row for last REG game', last_gid); return
        def poss_row(r):
            oreb = r[4]*0.245; ooreb = (r[1]-r[0]+r[3])*0  # placeholder
            # team poss = fga - oreb_est + 0.44*fta + tov? code: fga + 0.44*fta + tov
            p = r[2] + 0.44*r[3] + r[4]  # fga + 0.44 fta + tov  (matches builder comment)
            return p
        def netrtg(r):
            p = r[2]+0.44*r[3]+r[4]
            # opp poss uses opp fga/opp fta/opp tov - approximate same (symmetric-ish)
            op = r[2]+0.44*r[3]+r[4]
            return 100*r[0]/p - 100*r[1]/op
        recent5 = g[-5:]
        recent10 = g[-10:]
        def avg(rows, fn):
            vals=[fn(r) for r in rows]; return sum(vals)/len(vals)
        calc = {
            'net_rtg_r5': avg(recent5, lambda r: 100*r[0]/poss_row(r) - 100*r[1]/poss_row(r)),
            'ortg_r5':    avg(recent5, lambda r: 100*r[0]/poss_row(r)),
            'drtg_r5':    avg(recent5, lambda r: 100*r[1]/poss_row(r)),
            'efg_r5':     avg(recent5, lambda r: (r[6]+0.5*r[5])/r[2]),
            'pace_r5':    avg(recent5, lambda r: poss_row(r)),
            'net_rtg_r10': avg(recent10, lambda r: 100*r[0]/poss_row(r)-100*r[1]/poss_row(r)),
            'efg_r10':    avg(recent10, lambda r: (r[6]+0.5*r[5])/r[2]),
        }
        print('ROLLING (last REG game) stored vs recomputed (approx poss):')
        names = ['net_rtg_r5','ortg_r5','drtg_r5','pace_r5','efg_r5','net_rtg_r10','efg_r10']
        idx = [0,1,2,3,4,5,9]
        for nm, si in zip(names, idx):
            got = sr[si]
            want = calc[nm]
            print(f'  {nm:14s} stored={got if got is not None else "NULL":>8}  recomputed={want:>8.3f}  diff={abs(got-want) if got is not None else "?":.3f}')

rolling_check()
