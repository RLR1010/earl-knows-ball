"""Helper for saving training results to the database training_runs table.

Used by all sport/model training scripts to persist results JSON + pkl metadata
into the sport-specific schema's training_runs table.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

# Register UUID adapter
psycopg2.extras.register_uuid()

# Log SQL errors to a dedicated file so we can actually see them
log_path = "/tmp/db_training.log"
logger = logging.getLogger("db_training")
logger.setLevel(logging.DEBUG)
# Use FileHandler so each process appends, independent of any basicConfig
fh = logging.FileHandler(log_path, mode='a')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
logger.handlers.clear()
logger.addHandler(fh)
logger.propagate = False

# Also write to a session-specific file when possible
import atexit
def _log_flush():
    fh.flush()
atexit.register(_log_flush)


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)


def _get_conn():
    """Get a synchronous database connection."""
    return psycopg2.connect(DATABASE_URL)


def save_training_run(
    sport: str,
    model_type: str,
    results_json: Optional[dict | list],
    pkl_filename: str,
    algorithm: str = "xgboost",
    test_year: Optional[int] = None,
    train_years: Optional[list[int]] = None,
    description: Optional[str] = None,
) -> str:
    """Save a training run to the database and return the training_id (UUID string).

    Automatically clears the ``is_current`` flag for the same sport+model_type
    before inserting the new row and setting it as current.

    The returned training_id should also be used as the .pkl filename stem
    (e.g. ``{training_id}.pkl``).
    """
    training_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    train_years_str = ",".join(str(y) for y in train_years) if train_years else None

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Clear previous is_current for this model_type
            cur.execute(
                f'UPDATE {sport}.training_runs '
                f'SET is_current = FALSE '
                f'WHERE model_type = %s AND is_current = TRUE',
                (model_type,)
            )

            # Insert new run
            cur.execute(
                f'INSERT INTO {sport}.training_runs '
                f'(training_id, model_type, trained_at, results_json, is_current, '
                f' pkl_filename, algorithm, test_year, train_years, description) '
                f'VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s)',
                (
                    training_id,
                    model_type,
                    now,
                    psycopg2.extras.Json(results_json, dumps=lambda x: json.dumps(x, default=str)) if results_json else None,
                    pkl_filename,
                    algorithm,
                    test_year,
                    train_years_str,
                    description,
                )
            )
        conn.commit()
    finally:
        conn.close()

    return training_id


def update_pkl_filename(
    sport: str,
    training_id: str,
    pkl_filename: str,
) -> None:
    """Update the pkl_filename for an existing training run."""
    logger.debug(f"update_pkl_filename(sport={sport}, training_id={training_id}, pkl={pkl_filename})")
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            sql = f"UPDATE {sport}.training_runs SET pkl_filename = %s WHERE training_id = %s"
            logger.debug(f"  SQL: {sql} | pkl={pkl_filename}, training_id={training_id}")
            cur.execute(sql, (pkl_filename, training_id))
            logger.debug(f"  Updated {cur.rowcount} rows")
        conn.commit()
        logger.debug(f"  Commit OK")
    except Exception as e:
        logger.error(f"DB ERROR: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def get_model_pkl_path(sport: str, model_type: str, live: Optional[bool] = False) -> Optional[str]:
    """Get the full path to the current/live model's PKL file.

    Returns None if no model exists.
    """
    run = get_live_training_run(sport, model_type) if live else get_current_training_run(sport, model_type)
    if not run or not run.get("pkl_filename"):
        return None
    base = os.path.expanduser(
        f"~/.openclaw/workspace/earl-knows-football/data/models/{sport}"
    )
    return os.path.join(base, run["pkl_filename"])


def set_training_run_as_current(sport: str, run_id: int) -> Optional[dict]:
    """Set a specific training run as the current one for its model_type.

    Clears is_current for all other runs of the same model_type first,
    then updates the sport's features table to mark which features are
    current for this model type.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get the model_type and results_json of this run
            cur.execute(
                f'SELECT model_type, results_json FROM {sport}.training_runs WHERE id = %s',
                (run_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            model_type = row["model_type"]

            # ── Extract feature names from results_json ──
            # ATS stores feature_set (list of strings) per year-result
            # OU stores feature_importance (list of {feature, importance}) per year-result
            features: list[str] = []
            results = row.get("results_json")
            if results and isinstance(results, list):
                seen: set[str] = set()
                for yr in results:
                    if not isinstance(yr, dict):
                        continue
                    if model_type == "ats":
                        # ATS: try feature_set first, then fall back to feature_importance
                        fs = yr.get("feature_set", [])
                        if isinstance(fs, list) and fs:
                            for fname in fs:
                                if isinstance(fname, str) and fname not in seen:
                                    seen.add(fname)
                                    features.append(fname)
                        else:
                            fi = yr.get("feature_importance", [])
                            if isinstance(fi, list):
                                for entry in fi:
                                    fname = entry.get("feature") if isinstance(entry, dict) else None
                                    if fname and isinstance(fname, str) and fname not in seen:
                                        seen.add(fname)
                                        features.append(fname)
                    else:
                        # OU / any other model type that stores feature_importance
                        fi = yr.get("feature_importance", [])
                        if isinstance(fi, list):
                            for entry in fi:
                                fname = entry.get("feature") if isinstance(entry, dict) else None
                                if fname and isinstance(fname, str) and fname not in seen:
                                    seen.add(fname)
                                    features.append(fname)

            # ── Update training_runs is_current ──
            cur.execute(
                f'UPDATE {sport}.training_runs '
                f'SET is_current = FALSE '
                f'WHERE model_type = %s AND is_current = TRUE',
                (model_type,)
            )
            cur.execute(
                f'UPDATE {sport}.training_runs '
                f'SET is_current = TRUE '
                f'WHERE id = %s',
                (run_id,)
            )

            # ── Update sport.features table ──
            col = {"ou": "current_ou", "ats": "current_ats"}.get(model_type)
            if col:
                # Clear the column for all features first
                cur.execute(
                    f'UPDATE {sport}.features SET {col} = FALSE'
                )
                # Set it for features used in this training run
                if features:
                    placeholders = ",".join("%s" for _ in features)
                    cur.execute(
                        f'UPDATE {sport}.features SET {col} = TRUE '
                        f'WHERE name IN ({placeholders})',
                        features
                    )

        conn.commit()

        # Return the updated run
        return get_current_training_run(sport, model_type)
    finally:
        conn.close()


def set_training_run_as_live(sport: str, run_id: int) -> Optional[dict]:
    """Set a specific training run as the live (active prediction) one for its model_type.

    Clears is_live for all other runs of the same model_type first,
    then updates the sport's features table to mark which features are
    live for this model type.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get the model_type and results_json of this run
            cur.execute(
                f'SELECT model_type, results_json FROM {sport}.training_runs WHERE id = %s',
                (run_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            model_type = row["model_type"]

            # ── Extract feature names from results_json ──
            # ATS stores feature_set (list of strings) per year-result
            # OU stores feature_importance (list of {feature, importance}) per year-result
            features: list[str] = []
            results = row.get("results_json")
            if results and isinstance(results, list):
                seen: set[str] = set()
                for yr in results:
                    if not isinstance(yr, dict):
                        continue
                    # Try feature_set first (MLB format)
                    fs = yr.get("feature_set", [])
                    if isinstance(fs, list) and len(fs) > 0:
                        for fname in fs:
                            if isinstance(fname, str) and fname not in seen:
                                seen.add(fname)
                                features.append(fname)
                    # Fall back to feature_importance (NFL format — used by both ATS and OU)
                    if not features:
                        fi = yr.get("feature_importance", [])
                        if isinstance(fi, list):
                            fnames = []
                            for entry in fi:
                                fname = entry.get("feature") if isinstance(entry, dict) else None
                                if fname and isinstance(fname, str):
                                    fnames.append(fname)
                            if fnames:
                                for fname in fnames:
                                    if fname not in seen:
                                        seen.add(fname)
                                        features.append(fname)

                        # ── Fallback: input_features (new format — full list) ──
                        # If input_features is a list of strings, use it directly.
                        # (Replaces the old integer-count format that lost zero-importance features.)
                        if not features:
                            inp = yr.get("input_features", [])
                            if isinstance(inp, list) and len(inp) > 0 and isinstance(inp[0], str):
                                for fname in inp:
                                    if fname not in seen:
                                        seen.add(fname)
                                        features.append(fname)

                        # ── Last resort: load pkl to extract trained feature names ──
                        # feature_importance may drop zero-importance features (e.g. temp, wind in OU).
                        if not features:
                            cur.execute(
                                f"SELECT test_year, pkl_filename FROM {sport}.training_runs WHERE id = %s",
                                (run_id,),
                            )
                            r2 = cur.fetchone()
                            if r2 and r2["pkl_filename"]:
                                pkl_str = r2["pkl_filename"]
                                for pkl_part in [
                                    s.strip() for s in pkl_str.split(",") if s.strip()
                                ]:
                                    for base_dir in [
                                        f"data/models/{sport}",
                                        f"/home/rich/.openclaw/workspace/earl-knows-football/data/models/{sport}",
                                    ]:
                                        pkl_path = f"{base_dir}/{pkl_part}"
                                        if os.path.exists(pkl_path):
                                            try:
                                                import xgboost as xgb

                                                bst = xgb.Booster()
                                                bst.load_model(pkl_path)
                                                fn = bst.feature_names
                                                if fn:
                                                    for fname in fn:
                                                        if fname not in seen:
                                                            seen.add(fname)
                                                            features.append(fname)
                                                    logger.info(
                                                        "set_training_run_as_live: extracted %d features from pkl %s",
                                                        len(features),
                                                        pkl_part,
                                                    )
                                            except Exception as exc:
                                                logger.warning(
                                                    "set_training_run_as_live: pkl fallback failed for %s: %s",
                                                    pkl_path,
                                                    exc,
                                                )
                                            break
                                    if features:
                                        break

            # ── Update training_runs is_live ──
            cur.execute(
                f'UPDATE {sport}.training_runs '
                f'SET is_live = FALSE '
                f'WHERE model_type = %s AND is_live = TRUE',
                (model_type,)
            )
            cur.execute(
                f'UPDATE {sport}.training_runs '
                f'SET is_live = TRUE '
                f'WHERE id = %s',
                (run_id,)
            )

            # ── Update sport.features table (current_* = what frontend reads, live_* = legacy) ──
            current_col = {"ou": "current_ou", "ats": "current_ats"}.get(model_type)
            live_col = {"ou": "live_ou", "ats": "live_ats"}.get(model_type)
            if current_col:
                # Clear both columns for all features first
                cur.execute(
                    f'UPDATE {sport}.features SET {current_col} = FALSE'
                )
                if live_col:
                    cur.execute(
                        f'UPDATE {sport}.features SET {live_col} = FALSE'
                    )
                # Set it for features used in this training run
                if features:
                    placeholders = ",".join("%s" for _ in features)
                    cur.execute(
                        f'UPDATE {sport}.features SET {current_col} = TRUE '
                        f'WHERE name IN ({placeholders})',
                        features
                    )
                    if live_col:
                        cur.execute(
                            f'UPDATE {sport}.features SET {live_col} = TRUE '
                            f'WHERE name IN ({placeholders})',
                            features
                        )

        conn.commit()

        return get_current_training_run(sport, model_type)
    finally:
        conn.close()


def get_training_run(sport: str, run_id: int) -> Optional[dict]:
    """Get a single training run by ID, with full results_json."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f'SELECT * FROM {sport}.training_runs WHERE id = %s',
                (run_id,)
            )
            row = cur.fetchone()
            if row:
                row["training_id"] = str(row["training_id"])
                row["id"] = row["id"]  # is already int
                if hasattr(row["trained_at"], 'isoformat'):
                    row["trained_at"] = row["trained_at"].isoformat()
                return dict(row)
            return None
    finally:
        conn.close()


def get_current_training_run(
    sport: str,
    model_type: str,
) -> Optional[dict]:
    """Get the current (production) training run for a sport+model_type.

    Returns None if no current run exists.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f'SELECT * FROM {sport}.training_runs '
                f'WHERE model_type = %s AND is_current = TRUE '
                f'ORDER BY trained_at DESC LIMIT 1',
                (model_type,)
            )
            row = cur.fetchone()
            if row:
                row["training_id"] = str(row["training_id"])
                row["id"] = str(row["id"])
                if hasattr(row["trained_at"], 'isoformat'):
                    row["trained_at"] = row["trained_at"].isoformat()
                return dict(row)
            return None
    finally:
        conn.close()


def get_live_training_run(
    sport: str,
    model_type: str,
) -> Optional[dict]:
    """Get the live (active prediction) training run for a sport+model_type.

    Returns None if no live run exists.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f'SELECT * FROM {sport}.training_runs '
                f'WHERE model_type = %s AND is_live = TRUE '
                f'ORDER BY trained_at DESC LIMIT 1',
                (model_type,)
            )
            row = cur.fetchone()
            if row:
                row["training_id"] = str(row["training_id"])
                row["id"] = str(row["id"])
                if hasattr(row["trained_at"], 'isoformat'):
                    row["trained_at"] = row["trained_at"].isoformat()
                return dict(row)
            return None
    finally:
        conn.close()


def get_all_training_runs_for_model_type(
    sport: str,
    model_type: str,
    limit: int = 10,
) -> list[dict]:
    """Get the most recent training runs for a sport+model_type."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f'SELECT * FROM {sport}.training_runs '
                f'WHERE model_type = %s '
                f'ORDER BY trained_at DESC LIMIT %s',
                (model_type, limit)
            )
            rows = []
            for row in cur.fetchall():
                row["training_id"] = str(row["training_id"])
                row["id"] = str(row["id"])
                if hasattr(row["trained_at"], 'isoformat'):
                    row["trained_at"] = row["trained_at"].isoformat()
                rows.append(dict(row))
            return rows
    finally:
        conn.close()


def get_all_training_runs(
    sport: str,
    limit: int = 50,
) -> list[dict]:
    """Get the most recent training runs across all model types for a sport."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f'SELECT * FROM {sport}.training_runs '
                f'ORDER BY trained_at DESC LIMIT %s',
                (limit,)
            )
            rows = []
            for row in cur.fetchall():
                row["training_id"] = str(row["training_id"])
                row["id"] = str(row["id"])
                if hasattr(row["trained_at"], 'isoformat'):
                    row["trained_at"] = row["trained_at"].isoformat()
                rows.append(dict(row))
            return rows
    finally:
        conn.close()
