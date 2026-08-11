"""EXACT replication of the live MLB batch-predict feature path, to verify
whether the running live service is actually feeding the model the same
features the loader produces (i.e. is live == backtest-model on fresh picks).

Reads today's fresh live api picks and recomputes the OU model output using
the exact live assembly: all_historic FINAL (no season filter) + target_games
upcoming, build_features, extract, predict. No writes.
"""
import sys, asyncio
sys.path.insert(0, str("/home/rich/.openclaw/workspace/earl-knows-football/backend"))
import numpy as np, pandas as pd
from app.handicapping.mlb.data_loader import get_data_loader, build_features
from app.handicapping.mlb.mlb_engine import _load_model_for_year, _extract_feature_vector
from app.database import async_session
from sqlalchemy import text

def main():
    dl = get_data_loader()
    ou = _load_model_for_year("ou", 2026)
    ats = _load_model_for_year("ats", 2026)

    async def load():
        async with async_session() as db:
            return await db.execute(text("""
              SELECT gp.game_id, gp.predicted_total, gp.predicted_margin,
                     gp.ats_model_file, gp.ou_model_file
              FROM mlb.game_predictions gp
              WHERE gp.created_at >= '2026-08-11 15:00' AND gp.source='api'"""))
    stored = asyncio.run(load()).fetchall()
    gids = [r.game_id for r in stored]
    print(f"fresh live picks: {len(stored)}", flush=True)

    print("loading all historic FINAL (exact live path)...", flush=True)
    historic = dl.load_games(status="FINAL", include_upcoming=False)
    targets = dl.load_games(status=None, include_upcoming=True, game_ids=gids)
    df = build_features(pd.concat([historic, targets], ignore_index=True))
    by = {str(r["game_id"]): r for _, r in df.iterrows()}
    print(f"frame rows={len(df)}", flush=True)

    print(f"{'gid':>6} {'stored_pt':>9} {'model_pt':>8} {'stored_m':>8} {'model_m':>7} {'match':>5}", flush=True)
    d = 0
    for s in stored:
        r = by.get(str(s.game_id))
        if r is None:
            print(f"{s.game_id:>6} not-in-frame", flush=True); d += 1; continue
        fo = _extract_feature_vector(r, "ou"); fa = _extract_feature_vector(r, "ats")
        pt = float(ou.predict(np.asarray(fo)[np.newaxis, :])[0]) if fo is not None else None
        pm = float(ats.predict(np.asarray(fa)[np.newaxis, :])[0]) if fa is not None else None
        ok = pt is not None and abs(pt - float(s.predicted_total)) < 0.01
        if not ok: d += 1
        print(f"{s.game_id:>6} {s.predicted_total:>9} {('%.3f'%pt) if pt is not None else '-':>8} "
              f"{s.predicted_margin:>8} {('%.2f'%pm) if pm is not None else '-':>7} {'YES' if ok else 'NO':>5}", flush=True)
    print(f"\nMismatches: {d}/{len(stored)}", flush=True)

if __name__ == "__main__":
    main()
