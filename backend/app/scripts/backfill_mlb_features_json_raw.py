"""Backfill MLB pick-card feature JSON with clean scalar values.

Mirror of the NBA/NFL backfills (backfill_nba_features_json_raw.py /
backfill_nfl_features_json_raw.py): rebuilds the stored `features_json`
column on `mlb.game_predictions` from the current data loader + pick-card
feature metadata, so the game details page shows the latest feature names /
values / descriptions after the admin edits features in `/admin/features`.

MLB differs from NFL/NBA: its loader exposes `load_games(...)` +
`build_features(...)` rather than a `load_inference_data(game_ids=...)`, and
its pick-card metadata + inference columns are loaded through an async
session. So this script uses an asyncio driver.

It only writes the `features_json` column -- picks, predictions, and full
text are left untouched. Run as a script:

    PYTHONPATH=. python app/scripts/backfill_mlb_features_json_raw.py

or call `backfill_mlb_pick_card_json()` from code (e.g. the admin endpoint).
"""
import asyncio
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/rich/.openclaw/workspace/earl-knows-football/backend")

from app.handicapping.mlb.data_loader import MLBDataLoader, build_features  # noqa: E402
from app.handicapping.mlb.mlb_engine import (  # noqa: E402
    _extract_pick_card_features,
    _inference_feature_names,
    _load_pick_card_feature_metadata,
)
from app.database import admin_async_session  # noqa: E402

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


async def _backfill_async() -> dict:
    conn = psycopg2.connect(**DB_DSN)
    cur = conn.cursor()

    cur.execute(
        "SELECT game_id FROM mlb.game_predictions "
        "WHERE features_json IS NOT NULL ORDER BY game_id"
    )
    game_ids = [r[0] for r in cur.fetchall()]
    if not game_ids:
        print("No MLB prediction rows to backfill.")
        cur.close()
        conn.close()
        return {"updated": 0, "total": 0, "errors": 0, "elapsed_s": 0.0}

    t_start = time.time()

    # Load pick-card metadata + inference column list through the async session.
    async with admin_async_session() as db:
        infer_cols = await _inference_feature_names(db)
        pc_feats = await _load_pick_card_feature_metadata(db)
    cur.close()
    conn.close()

    print(f"pick-card feature metadata: {len(pc_feats)} features")

    dl = MLBDataLoader()
    chunk = 750
    updated = 0
    errors = 0

    for i in range(0, len(game_ids), chunk):
        batch = game_ids[i : i + chunk]
        t0 = time.time()
        try:
            # Build the full feature df for this batch (mirrors the engine path).
            combined = dl.load_games(game_ids=batch, columns=infer_cols)
            df = build_features(combined)
        except Exception as e:  # noqa: BLE001
            print(f"[batch {i}] load failed: {e!r}")
            errors += len(batch)
            continue

        if "game_id" not in df.columns and "game_id" not in df.index.names:
            print(f"[batch {i}] no game_id; skipping")
            errors += len(batch)
            continue

        conn = psycopg2.connect(**DB_DSN)
        cur = conn.cursor()
        for gid in batch:
            row = df[df["game_id"].astype(str) == str(gid)]
            if row.empty:
                continue
            row_s = row.iloc[0]
            try:
                feats_json = _extract_pick_card_features(row_s, pc_feats)
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  extract failed for {gid}: {e!r}")
                continue
            if feats_json:
                cur.execute(
                    "UPDATE mlb.game_predictions SET features_json=%s WHERE game_id=%s",
                    (feats_json, int(gid)),
                )
                updated += 1
        conn.commit()
        cur.close()
        conn.close()

        el = time.time() - t0
        print(
            f"[{i}:{i + len(batch)}] updated {updated} so far, "
            f"batch in {el:.1f}s ({len(batch) / el:.1f}/s)"
        )

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


async def backfill_mlb_pick_card_json_async() -> dict:
    """Async entry point (for use inside a running event loop, e.g. the admin endpoint)."""
    return await _backfill_async()


def backfill_mlb_pick_card_json() -> dict:
    """Rebuild `features_json` for every MLB prediction row that has one."""
    return asyncio.run(_backfill_async())


if __name__ == "__main__":
    sys.exit(backfill_mlb_pick_card_json())
