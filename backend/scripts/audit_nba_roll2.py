"""Final VERIFIED rolling recompute for OKC 2024 last REG game, using the EXACT
builder formula (populate_team_rolling_stats.py):
  oreb_est = reb*0.245 ; poss = fga - oreb_est + 0.44*fta
  ortg = 100*pts/poss ; drtg = 100*opp/opp_poss ; pace = (poss+opp_poss)/2
  net_rtg = ortg - drtg ; efg = (fgm + 0.5*fgm3)/fga
Windows are INCLUSIVE of the target row (team_game).
"""
from sqlalchemy import create_engine, text
ENGINE = create_engine('postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football')

def main():
    with ENGINE.connect() as c:
        team_id, yr = 24, 2024
        sid = c.execute(text('SELECT id FROM nba.seasons WHERE year=:y'), {'y':yr}).scalar()
        # per-team-game raw rows for REG, chronologically, with the cols the builder uses
        g = c.execute(text('''
            SELECT g.id AS game_id, g.date,
                   CASE WHEN g.home_team_id=:t THEN g.home_score            ELSE g.away_score END AS pts,
                   CASE WHEN g.home_team_id=:t THEN g.away_score            ELSE g.home_score END AS opp,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_attempted ELSE g.away_field_goals_attempted END AS fga,
                   CASE WHEN g.home_team_id=:t THEN g.home_free_throws_attempted ELSE g.away_free_throws_attempted END AS fta,
                   CASE WHEN g.home_team_id=:t THEN g.home_rebounds         ELSE g.away_rebounds END AS reb,
                   CASE WHEN g.home_team_id=:t THEN g.away_rebounds         ELSE g.home_rebounds END AS opp_reb,
                   CASE WHEN g.home_team_id=:t THEN g.home_three_points_made ELSE g.away_three_points_made END AS fgm3,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_made ELSE g.away_field_goals_made END AS fgm
            FROM nba.games g
            WHERE g.season_id=:s AND g.game_type='REG' AND g.status='FINAL'
              AND (g.home_team_id=:t OR g.away_team_id=:t)
            ORDER BY g.date ASC, g.id ASC
        '''), {'t': team_id, 's': sid}).fetchall()
        rows = [dict(zip(['game_id','date','pts','opp','fga','fta','reb','opp_reb','fgm3','fgm'], r)) for r in g]

        def per(gg):
            oreb = gg['reb']*0.245
            ooreb = gg['opp_reb']*0.245
            poss = gg['fga'] - oreb + 0.44*gg['fta']
            opp_poss = gg['opp'] and (gg['opp']-0)  # placeholder
            # opp_poss needs opp fga/fta/reb -> approximate: opp_poss = opp poss via opp fga? we don't have opp fga here
            return poss
        # We lack opp_fga/opp_fta in this select; add them for opp_poss
        # -> rebuild select with opp_fga/opp_fta
        c.execute(text('DROP VIEW IF EXISTS _t'))
        g2 = c.execute(text('''
            SELECT g.id AS game_id, g.date,
                   CASE WHEN g.home_team_id=:t THEN g.home_score ELSE g.away_score END AS pts,
                   CASE WHEN g.home_team_id=:t THEN g.away_score ELSE g.home_score END AS opp,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_attempted ELSE g.away_field_goals_attempted END AS fga,
                   CASE WHEN g.home_team_id=:t THEN g.home_free_throws_attempted ELSE g.away_free_throws_attempted END AS fta,
                   CASE WHEN g.home_team_id=:t THEN g.home_rebounds ELSE g.away_rebounds END AS reb,
                   CASE WHEN g.home_team_id=:t THEN g.away_rebounds ELSE g.home_rebounds END AS opp_reb,
                   CASE WHEN g.home_team_id=:t THEN g.away_field_goals_attempted ELSE g.home_field_goals_attempted END AS opp_fga,
                   CASE WHEN g.home_team_id=:t THEN g.away_free_throws_attempted ELSE g.home_free_throws_attempted END AS opp_fta,
                   CASE WHEN g.home_team_id=:t THEN g.home_three_points_made ELSE g.away_three_points_made END AS fgm3,
                   CASE WHEN g.home_team_id=:t THEN g.home_field_goals_made ELSE g.away_field_goals_made END AS fgm
            FROM nba.games g
            WHERE g.season_id=:s AND g.game_type='REG' AND g.status='FINAL'
              AND (g.home_team_id=:t OR g.away_team_id=:t)
            ORDER BY g.date ASC, g.id ASC
        '''), {'t': team_id, 's': sid}).fetchall()
        f = lambda x: 0 if x is None else float(x)
        rows = [dict(zip(['game_id','date','pts','opp','fga','fta','reb','opp_reb','opp_fga','opp_fta','fgm3','fgm'], r)) for r in g2]
        for gg in rows:
            for k in ['pts','opp','fga','fta','reb','opp_reb','opp_fga','opp_fta','fgm3','fgm']:
                gg[k]=f(gg[k])
        def pnt(gg):
            poss = gg['fga'] - gg['reb']*0.245 + 0.44*gg['fta']
            opp_poss = gg['opp_fga'] - gg['opp_reb']*0.245 + 0.44*gg['opp_fta']
            return poss, opp_poss
        def netrtg5(rows):
            vals=[]
            for gg in rows[-5:]:
                poss, opp_poss = pnt(gg)
                if poss>0 and opp_poss>0:
                    vals.append(100*gg['pts']/poss - 100*gg['opp']/opp_poss)
            return sum(vals)/len(vals)
        def ortg5(rows):
            vals=[]
            for gg in rows[-5:]:
                poss,_=pnt(gg)
                if poss>0: vals.append(100*gg['pts']/poss)
            return sum(vals)/len(vals)
        def pace5(rows):
            vals=[]
            for gg in rows[-5:]:
                poss,opp_poss=pnt(gg)
                if poss>0 and opp_poss>0: vals.append((poss+opp_poss)/2)
            return sum(vals)/len(vals)
        def efg5(rows):
            vals=[]
            for gg in rows[-5:]:
                if gg['fga']>0: vals.append((gg['fgm']+0.5*gg['fgm3'])/gg['fga'])
            return sum(vals)/len(vals)

        last = rows[-1]
        # stored rolling row for last game
        sr = c.execute(text('''
            SELECT net_rtg_r5, ortg_r5, drtg_r5, pace_r5, efg_r5
            FROM nba.team_rolling_stats WHERE team_id=:t AND season_id=:s AND game_id=:gid
        '''), {'t':team_id,'s':sid,'gid':last['game_id']}).fetchone()
        got = {'net_rtg_r5':sr[0],'ortg_r5':sr[1],'drtg_r5':sr[2],'pace_r5':sr[3],'efg_r5':sr[4]}
        calc = {
            'net_rtg_r5': netrtg5(rows), 'ortg_r5': ortg5(rows), 'pace_r5': pace5(rows), 'efg_r5': efg5(rows),
        }
        gid = last['game_id']
        print(f'OKC {yr} last REG game {gid}: stored vs exact-builder recompute')
        for k in ['net_rtg_r5','ortg_r5','pace_r5','efg_r5']:
            print(f'  {k:12s} stored={got[k]:>8.4f}  recomputed={calc[k]:>8.4f}  diff={abs(got[k]-calc[k]):.4f}')

if __name__=='__main__':
    main()
