"""Evaluate combined margin + OU score predictions across all years."""
import pandas as pd
import numpy as np

YEARS = [2021, 2022, 2023, 2024, 2025]

all_games = []
for year in YEARS:
    m = pd.read_csv(f'/app/data/nfl_backtest_{year}.csv')
    o = pd.read_csv(f'/app/data/ou_baseline_{year}.csv')
    # Merge on game_id
    df = m[['game_id', 'week', 'pred_margin', 'actual_margin', 'hpf', 'hpa', 'apf', 'apa']].merge(
        o[['game_id', 'pred_total', 'actual_total']], on='game_id')
    
    # Combined predictions
    df['pred_home'] = (df['pred_total'] + df['pred_margin']) / 2
    df['pred_away'] = (df['pred_total'] - df['pred_margin']) / 2
    df['actual_home'] = (df['actual_total'] + df['actual_margin']) / 2
    df['actual_away'] = (df['actual_total'] - df['actual_margin']) / 2
    
    # Home error
    df['home_err'] = df['pred_home'] - df['actual_home']
    df['away_err'] = df['pred_away'] - df['actual_away']
    df['total_err'] = df['pred_total'] - df['actual_total']
    df['margin_err'] = df['pred_margin'] - df['actual_margin']
    
    df['year'] = year
    all_games.append(df)

all_df = pd.concat(all_games)

for year in YEARS:
    d = all_df[all_df.year == year]
    n = len(d)
    print(f'{"="*60}')
    print(f'  {year} — Combined Score Predictions ({n} games)')
    print(f'{"="*60}')
    
    # Total MAE
    total_mae = abs(d['total_err']).mean()
    margin_mae = abs(d['margin_err']).mean()
    home_mae = abs(d['home_err']).mean()
    away_mae = abs(d['away_err']).mean()
    
    print(f'  MAE — Total: {total_mae:.1f}  Margin: {margin_mae:.1f}  Home: {home_mae:.1f}  Away: {away_mae:.1f}')
    
    # Total error distribution
    print(f'  Total error distribution:')
    und = (d['total_err'] < 0).sum()
    ovr = (d['total_err'] > 0).sum()
    und_m = d.loc[d['total_err'] < 0, 'total_err'].mean() if und else 0
    ovr_m = d.loc[d['total_err'] > 0, 'total_err'].mean() if ovr else 0
    print(f'    Under: {und:>3}/{n} ({und/n*100:.1f}%)  avg {abs(und_m):.1f} low')
    print(f'    Over:  {ovr:>3}/{n} ({ovr/n*100:.1f}%)  avg {abs(ovr_m):.1f} high')
    
    for lo, hi, lbl in [(0,3, "0-3"), (3,7, "3-7"), (7,14,"7-14"), (14,21,"14-21"), (21,999,"21+")]:
        cnt = ((abs(d['total_err']) >= lo) & (abs(d['total_err']) < hi)).sum()
        print(f'    {cnt:>3} ({cnt/n*100:5.1f}%)  {lbl} pts off target total')
    
    # Home/Away score MAE
    print(f'  Home score MAE:  {home_mae:.1f}')
    print(f'  Away score MAE:  {away_mae:.1f}')
    
    # O/U accuracy
    ou = d['pred_total'] - d['actual_total']
    pushes = (abs(ou) < 0.5).sum()
    correct = ((d['pred_total'] > d['actual_total']) == (d['actual_total'] > 0)).sum()  # placeholder
    # Better O/U eval: compare predicted over/under vs actual over/under around... wait, we don't have Vegas line here
    # Let's compute total prediction direction: were we directionally right about high or low scoring?
    avg_total = d['actual_total'].mean()
    dir_correct = ((d['pred_total'] > avg_total) == (d['actual_total'] > avg_total)).sum()
    print(f'  Directional (pred > avg vs actual > avg): {dir_correct}/{n} ({dir_correct/n*100:.1f}%)')
    print()

print(f'{"="*60}')
print(f'  ALL YEARS COMBINED ({len(all_df)} games)')
print(f'{"="*60}')

d = all_df
n = len(d)

total_mae = abs(d['total_err']).mean()
margin_mae = abs(d['margin_err']).mean()
home_mae = abs(d['home_err']).mean()
away_mae = abs(d['away_err']).mean()

print(f'  MAE — Total: {total_mae:.1f}  Margin: {margin_mae:.1f}  Home: {home_mae:.1f}  Away: {away_mae:.1f}')
print(f'  RMSE — Total: {(d.total_err**2).mean()**0.5:.1f}')
print(f'  Total error: μ={d.total_err.mean():+.1f}  σ={d.total_err.std():.1f}')

und = (d['total_err'] < 0).sum()
ovr = (d['total_err'] > 0).sum()
und_m = d.loc[d['total_err'] < 0, 'total_err'].mean()
ovr_m = d.loc[d['total_err'] > 0, 'total_err'].mean()
print(f'  Under: {und:>3}/{n} ({und/n*100:.1f}%)  avg miss {abs(und_m):.1f} pts')
print(f'  Over:  {ovr:>3}/{n} ({ovr/n*100:.1f}%)  avg miss {abs(ovr_m):.1f} pts')

for lo, hi, lbl in [(0,3, "0-3pts"), (3,7, "3-7pts"), (7,14,"7-14pts"), (14,21,"14-21pts"), (21,999,"21+pts")]:
    cnt = ((abs(d['total_err']) >= lo) & (abs(d['total_err']) < hi)).sum()
    print(f'  {cnt:>3} ({cnt/n*100:5.1f}%)  {lbl}')

# Compare combined vs heuristic
print()
print(f'  Combined vs Heuristic PPG blend:')
d['heuristic_total'] = (d['hpf'] + d['apa'] + d['apf'] + d['hpa']) / 2
d['heuristic_err'] = d['heuristic_total'] - d['actual_total']
combined_mae = abs(d['total_err']).mean()
heuristic_mae = abs(d['heuristic_err']).mean()
print(f'    Combined MAE:  {combined_mae:.2f}')
print(f'    Heuristic MAE: {heuristic_mae:.2f}')
print(f'    Improvement:   {heuristic_mae - combined_mae:+.2f} pts')

# Home/away score accuracy
print()
print(f'  Individual score accuracy:')
print(f'    Home score MAE:  {abs(d.home_err).mean():.1f}')
print(f'    Away score MAE:  {abs(d.away_err).mean():.1f}')
print(f'    Home σ:          {d.home_err.std():.1f}  Away σ: {d.away_err.std():.1f}')
