import pandas as pd
import numpy as np

for year in [2021, 2022, 2023, 2024, 2025]:
    m = pd.read_csv(f'/app/data/nfl_backtest_{year}.csv')
    o = pd.read_csv(f'/app/data/ou_baseline_{year}.csv')
    
    print(f'{"="*50}')
    print(f'  {year} SEASON')
    print(f'{"="*50}')
    
    m['error'] = m.pred_margin - m.actual_margin
    n = len(m)
    under = int((m.error < 0).sum())
    over = int((m.error > 0).sum())
    und_mean = float(m.loc[m.error < 0, 'error'].mean()) if under else 0
    ovr_mean = float(m.loc[m.error > 0, 'error'].mean()) if over else 0
    
    print(f'── Spread (Margin) Model ──')
    print(f'  MAE:  {abs(m.error).mean():>.1f}  RMSE:  {(m.error**2).mean()**0.5:.1f}')
    print(f'  Mean: {m.error.mean():+.1f}  Std:   {m.error.std():.1f}')
    print(f'  Under: {under:>3}/{n} ({under/n*100:.1f}%)  avg off by {abs(und_mean):.1f} pts')
    print(f'  Over:  {over:>3}/{n} ({over/n*100:.1f}%)  avg off by {abs(ovr_mean):.1f} pts')
    
    o['error'] = o.pred_total - o.actual_total
    n2 = len(o)
    u2 = int((o.error < 0).sum())
    ov2 = int((o.error > 0).sum())
    u2m = float(o.loc[o.error < 0, 'error'].mean()) if u2 else 0
    ov2m = float(o.loc[o.error > 0, 'error'].mean()) if ov2 else 0
    
    print(f'── OU (Total) Model ──')
    print(f'  MAE:  {abs(o.error).mean():>.1f}  RMSE:  {(o.error**2).mean()**0.5:.1f}')
    print(f'  Mean: {o.error.mean():+.1f}  Std:   {o.error.std():.1f}')
    print(f'  Under: {u2:>3}/{n2} ({u2/n2*100:.1f}%)  avg off by {abs(u2m):.1f} pts')
    print(f'  Over:  {ov2:>3}/{n2} ({ov2/n2*100:.1f}%)  avg off by {abs(ov2m):.1f} pts')
    
    # Error buckets
    print(f'  Error distribution:')
    for lo, hi, lbl in [(0,3,"0-3pts"), (3,7,"3-7pts"), (7,14,"7-14pts"), (14,21,"14-21pts"), (21,999,"21+pts")]:
        cnt = int(((abs(o.error) >= lo) & (abs(o.error) < hi)).sum())
        print(f'    {cnt:3d} ({cnt/n2*100:4.1f}%)  {lbl}')
    
    print()

# Combined
print(f'{"="*50}')
print('  ALL YEARS COMBINED (1359 games)')
print(f'{"="*50}')
all_m = pd.concat([pd.read_csv(f'/app/data/nfl_backtest_{y}.csv') for y in [2021,2022,2023,2024,2025]])
all_o = pd.concat([pd.read_csv(f'/app/data/ou_baseline_{y}.csv') for y in [2021,2022,2023,2024,2025]])
all_m['error'] = all_m.pred_margin - all_m.actual_margin
all_o['error'] = all_o.pred_total - all_o.actual_total

for name, d in [('Spread', all_m), ('OU Total', all_o)]:
    n = len(d)
    und = int((d.error < 0).sum())
    ovr = int((d.error > 0).sum())
    und_m = float(d.loc[d.error < 0, 'error'].mean()) if und else 0
    ovr_m = float(d.loc[d.error > 0, 'error'].mean()) if ovr else 0
    print(f'── {name} Model ──')
    print(f'  MAE: {abs(d.error).mean():.1f}  RMSE: {(d.error**2).mean()**0.5:.1f}')
    print(f'  Mean: {d.error.mean():+.1f}  Std: {d.error.std():.1f}')
    print(f'  Under: {und}/{n} ({und/n*100:.1f}%)  avg off by {abs(und_m):.1f} pts')
    print(f'  Over:  {ovr}/{n} ({ovr/n*100:.1f}%)  avg off by {abs(ovr_m):.1f} pts')
