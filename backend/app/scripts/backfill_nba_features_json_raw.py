"""Backfill NBA pick-card feature JSON with clean scalar values.

Root cause of corruption (2026-08-24): the NBA loader's `_build_features`
returned DUPLICATE `*_raw` columns (raw twin columns added to `keep` twice).
As a result `row["a_ats_margin_10_raw"]` resolved to a 2-element pandas Series
instead of a scalar, and `_extract_pick_card_features` stored that Series'
str() repr as the feature value -- e.g.

    a_ats_margin_10_raw    3.1
    a_ats_margin_10_raw    3.1
    Name: 23222, dtype: object

The loader bug is fixed. This script rebuilds `features_json` for every existing
NBA prediction row from the fixed loader, so already-predicted games display
clean numbers again. It only touches the `features_json` column -- picks,
predictions, and full text are left untouched.
"""
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/rich/.openclaw/workspace/earl-knows-football/backend")

from app.handicapping.nba.data_loader import NBADataLoader  # noqa: E402
from app.handicapping.nba.nba_engine import (  # noqa: E402
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


def _backfill() -> dict:
    conn = psycopg2.connect(**DB_DSN)
    cur = conn.cursor()

    cur.execute(
        "SELECT game_id FROM nba.game_predictions "
        "WHERE features_json IS NOT NULL ORDER BY game_id"
    )
    game_ids = [r[0] for r in cur.fetchall()]
    if not game_ids:
        print("No NBA prediction rows to backfill.")
        cur.close()
        conn.close()
        return {"updated": 0, "total": 0, "errors": 0, "elapsed_s": 0.0}

    pc_feats = _load_pick_card_feature_metadata()
    print(f"pick-card feature metadata: {len(pc_feats)} features")

    dl = NBADataLoader()
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
                    "UPDATE nba.game_predictions SET features_json=%s WHERE game_id=%s",
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


def backfill_nba_pick_card_json() -> dict:
    """Rebuild `features_json` for every NBA prediction row that has one."""
    return _backfill()


def main() -> int:
    _backfill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
