"""Backfill NFL pick-card feature JSON with clean scalar values.

Mirror of the NBA backfill (backfill_nba_features_json_raw.py): rebuilds the
stored `features_json` column on `nfl.game_predictions` from the (current)
data loader + pick-card feature metadata, so the game details page shows the
latest feature names/values/descriptions after the admin edits features in
`/admin/features`.

It only writes the `features_json` column -- picks, predictions, and full
text are left untouched. Run as a script:

    PYTHONPATH=. python app/scripts/backfill_nfl_features_json_raw.py

or call `backfill_nfl_pick_card_json()` from code (e.g. the admin endpoint).
"""
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/rich/.openclaw/workspace/earl-knows-football/backend")

from app.handicapping.nfl.data_loader import NFLDataLoader  # noqa: E402
from app.handicapping.nfl.engine import (  # noqa: E402
    _extract_pick_card_features,
    _load_pick_card_feature_metadata,
)

import psycopg2  # noqa: E402
from psycopg2.extensions import parse_dsn as _parse_dsn  # noqa: E402

_from_dotenv = os.path.join(os.path.dirname(__file__), "../../.env")
if os.path.exists(_from_dotenv):
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(_from_dotenv)
_async_url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
DB_DSN = _parse_dsn(_async_url) if _async_url else None
if DB_DSN is None:
    raise SystemExit("DATABASE_URL not set in backend/.env")


def backfill_nfl_pick_card_json() -> dict:
    """Rebuild `features_json` for every NFL prediction row that has one.

    Returns a summary dict: {updated, total, errors, elapsed_s}.
    """
    conn = psycopg2.connect(**DB_DSN)
    cur = conn.cursor()

    cur.execute(
        "SELECT game_id FROM nfl.game_predictions "
        "WHERE features_json IS NOT NULL ORDER BY game_id"
    )
    game_ids = [r[0] for r in cur.fetchall()]
    if not game_ids:
        print("No NFL prediction rows to backfill.")
        cur.close()
        conn.close()
        return {"updated": 0, "total": 0, "errors": 0, "elapsed_s": 0.0}

    pc_feats = _load_pick_card_feature_metadata()
    print(f"pick-card feature metadata: {len(pc_feats)} features")

    dl = NFLDataLoader()
    chunk = 750
    updated = 0
    errors = 0
    t_start = time.time()

    for i in range(0, len(game_ids), chunk):
        batch = game_ids[i : i + chunk]
        t0 = time.time()
        try:
            df = dl.load_inference_data(game_ids=batch)
        except Exception as e:  # noqa: BLE001
            print(f"[batch {i}] load failed: {e!r}")
            errors += len(batch)
            continue

        if "game_id" not in df.columns:
            print(f"[batch {i}] no game_id column; skipping")
            errors += len(batch)
            continue
        dfi = df.set_index("game_id", drop=False)
        for gid in batch:
            if gid not in dfi.index:
                continue
            row = dfi.loc[gid]
            if not isinstance(row, type(df)):
                try:
                    feats_json = _extract_pick_card_features(row, pc_feats)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    print(f"  extract failed for {gid}: {e!r}")
                    continue
            else:
                feats_json = None  # ambiguous multiple rows; skip
            if feats_json:
                cur.execute(
                    "UPDATE nfl.game_predictions SET features_json=%s WHERE game_id=%s",
                    (feats_json, int(gid)),
                )
                updated += 1

        conn.commit()
        el = time.time() - t0
        print(
            f"[{i}:{i + len(batch)}] updated {updated} so far, "
            f"batch in {el:.1f}s ({len(batch) / el:.1f}/s)"
        )

    cur.close()
    conn.close()
    result = {
        "updated": updated,
        "total": len(game_ids),
        "errors": errors,
        "elapsed_s": round(time.time() - t_start, 1),
    }
    print(
        f"DONE: {updated}/{len(game_ids)} rows updated, {errors} errors, "
        f"in {result['elapsed_s']}s"
    )
    return result


if __name__ == "__main__":
    sys.exit(backfill_nfl_pick_card_json())
