"""Compare the FULL stat profile of a completed MLB game vs a scheduled game.

Loads both through the authoritative GAME_QUERY (same data source the models
use) and diffs every column to spot anything missing / inconsistent for
scheduled games (pre-game).

Usage: python compare_completed_vs_scheduled.py <completed_gid> <scheduled_gid>
"""
import sys
sys.path.insert(0, str("/home/rich/.openclaw/workspace/earl-knows-football/backend"))
import pandas as pd
from app.handicapping.mlb.data_loader import get_data_loader

COMPLETED = int(sys.argv[1]) if len(sys.argv) > 1 else 48868
SCHEDULED = int(sys.argv[2]) if len(sys.argv) > 2 else 48869


def main():
    dl = get_data_loader()
    # load both as FINAL-ish / regular; scheduled may be SCHEDULED status
    try:
        c = dl.load_games(seasons=[2026], status=None, include_upcoming=True, game_ids=[COMPLETED])
    except Exception:
        c = dl.load_games(status="FINAL", game_ids=[COMPLETED])
    try:
        s = dl.load_games(seasons=[2026], status=None, include_upcoming=True, game_ids=[SCHEDULED])
    except Exception:
        s = dl.load_games(status=None, include_upcoming=True, game_ids=[SCHEDULED])

    if c.empty or s.empty:
        print(f"ERROR: completed empty={c.empty} scheduled empty={s.empty}")
        return
    cr = c.iloc[0]
    sr = s.iloc[0]
    print(f"Completed gid={COMPLETED} status={cr.get('status')} | Scheduled gid={SCHEDULED} status={sr.get('status')}")
    print(f"Completed: {cr.get('away_abbr')} @ {cr.get('home_abbr')} | Scheduled: {sr.get('away_abbr')} @ {sr.get('home_abbr')}")
    print("=" * 90)

    cols_c = set(cr.index); cols_s = set(sr.index)
    print(f"completed cols={len(cols_c)}  scheduled cols={len(cols_s)}  "
          f"only-in-completed={len(cols_c-cols_s)}  only-in-scheduled={len(cols_s-cols_c)}")
    if cols_c != cols_s:
        print("  ONLY IN COMPLETED:", sorted(cols_c-cols_s)[:40])
        print("  ONLY IN SCHEDULED:", sorted(cols_s-cols_c)[:40])

    print("\n### Column-by-column value comparison ###")
    n=0; missing=0; diff=0; same=0
    for col in sorted(set(cols_c) & set(cols_s)):
        vc, vs = cr[col], sr[col]
        # mark NaN/None as "MISSING"
        vc_nan = vc is None or (isinstance(vc,float) and pd.isna(vc)) or (hasattr(vc,'__len__') and len(vc)==0)
        vs_nan = vs is None or (isinstance(vs,float) and pd.isna(vs)) or (hasattr(vs,'__len__') and len(vs)==0)
        iseq = (vc==vs) or (vc_nan and vs_nan)
        if iseq:
            same+=1; continue
        n+=1
        # Determine if the scheduled is missing a value the completed has
        if not vc_nan and vs_nan:
            missing+=1
            print(f"  [MISSING-scheduled] {col:<28} completed={vc!r}  scheduled=NaN")
        elif vc_nan and not vs_nan:
            print(f"  [extra-scheduled]    {col:<28} completed=NaN scheduled={vs!r}")
        else:
            diff+=1
            vcs, vss = str(vc)[:40], str(vs)[:40]
            r = f"  [DIFF]               {col:<28} completed={vcs!r}  scheduled={vss!r}"
            # highlight structurally important ones more visibly
            if any(k in col for k in ("cum","_avg","_slg","_obp","_whip","_era","rest","pitcher","wins","losses","_ip","_k","confidence","_q","_o","_d","_bb","_so","_h","_r","home","away","surface","roof","temp","wind","venue","_last_","_5","_10","_ytd","_today")):
                r = "  " + r
            print(r)
    print("="*90)
    print(f"RESULT: same={same}  different={diff}  [scheduled MISSING values completed has]={missing}")
    print(f"  -> {len({col for col in sorted(set(cols_c)&set(cols_s))})} shared columns compared")


if __name__ == "__main__":
    main()
