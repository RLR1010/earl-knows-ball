"""Prove the two-path venue ERA seam:
  - MODEL input sees the imputed fallback (home/road ERA) when venue ERA is NULL.
  - PICK-CARD JSON stores the RAW value (real or null), never the fallback.
"""
import sys
sys.path.insert(0, str("/home/rich/.openclaw/workspace/earl-knows-football/backend"))
import numpy as np


def main():
    from app.handicapping.mlb.data_loader import get_data_loader
    from app.handicapping.mlb import mlb_engine as eng

    dl = get_data_loader()

    # Find games where a starting pitcher has 0 venue starts (venue ERA NULL).
    # Use the loader's own columns: h/a_pitcher_venue_era and h/a_pitcher_venue_starts.
    df = dl.load_games(seasons=[2026], status=None, include_upcoming=True,
                      game_ids=None, limit=None) if False else None

    # Simpler: directly grab a few scheduled + recent games and inspect rows.
    import asyncpg, asyncio

    async def ids():
        c = await asyncpg.connect("postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")
        rows = await c.fetch("""
          SELECT id FROM mlb.games
          WHERE (date >= now() - interval '1 day' AND date < now() + interval '1 day')
            AND status IN ('SCHEDULED','FINAL')
          ORDER BY date LIMIT 12""")
        await c.close()
        return [r['id'] for r in rows]

    gids = asyncio.run(ids())
    df = dl.load_games(seasons=[2026], status=None, include_upcoming=True, game_ids=gids)
    print(f"loaded {len(df)} rows")

    hit = 0
    for _, row in df.iterrows():
        h = row.get("h_pitcher_venue_era"); a = row.get("a_pitcher_venue_era")
        hs = row.get("h_pitcher_venue_starts"); as_ = row.get("a_pitcher_venue_starts")
        def nan_bool(x): return x is None or (isinstance(x, float) and np.isnan(x))
        # look for the away pitcher with 0 venue history (var venue ERA = NaN)
        if nan_bool(a) and (nan_bool(as_) or as_ == 0):
            hit += 1
            print(f"\n[away 0 venue starts] gid={row.get('game_id')} status={row.get('status')}")
            rawa = a  # raw feature should be NaN/null when starts=0

            # 1) What the MODEL sees (imputation path)
            feats = eng._extract_feature_vector(row, "ou")
            ou_cols = eng._get_features()["ou"]
            if "a_pitcher_venue_era" in ou_cols:
                i = ou_cols.index("a_pitcher_venue_era")
                model_val = feats[i]
                road = row.get("a_p_road_era_ytd")
                print(f"   raw(row)= {rawa!r}  starts=0")
                print(f"   MODEL sees: {model_val:.3f}")
                if road is not None and not nan_bool(road) and abs(model_val - float(road)) < 1e-6:
                    print(f"   ✓ model fallback == away road ERA ({float(road):.3f})  [GOOD: not 0]")
                elif model_val == 0.0:
                    print("   ✗ model got 0 — fallback NOT applied!")
                else:
                    print(f"   ? model fallback {model_val:.3f} vs road ERA {road!r}")
            else:
                print("   (a_pitcher_venue_era not in current_ou feature set)")

            # 2) What the PICK CARD stores (raw dict path) via _extract_pick_card_features
            meta = {}
            try:
                import asyncio as _aio
                meta = _aio.run(eng._load_pick_card_feature_metadata(None)) if False else {}
            except Exception:
                pass
            # minimal local metadata for the venue_era feature
            for cand in ("a_pitcher_venue_era", "h_pitcher_venue_era"):
                if cand not in meta:
                    meta[cand] = {"display_name": cand.replace("_", " ").title(), "description": cand}
            try:
                pc_json = eng._extract_pick_card_features(row, meta)  # row is a Series, supports .index
                # search the JSON for venue_era
                import json
                pc = json.loads(pc_json) if pc_json else []
                found = False
                for fe in pc if isinstance(pc, list) else pc.get("features", []):
                    name = fe.get("name") or fe.get("key") or ""
                    if "venue" in str(name).lower() and "era" in str(name).lower():
                        found = True
                        print(f"   pick-card[{name}] = {fe.get('value')!r}  (raw row value: {rawa!r})")
                if not found:
                    print("   (a_pitcher_venue_era not in pick_card feature set; listing any venue keys)")
            except Exception as e:
                print(f"   _extract_pick_card_features error: {e}")
            break
    if hit == 0:
        print("\nNo away-0-start game found in sample; verifying via raw-only inspect.")
        for _, row in df.iterrows():
            a = row.get("a_pitcher_venue_era"); as_ = row.get("a_pitcher_venue_starts")
            nan_bool = a is None or (isinstance(a, float) and np.isnan(a))
            print(f"  gid={row.get('game_id')} away_ven_era={a!r} starts={as_!r}")
    print("\nDONE")

if __name__ == "__main__":
    main()
