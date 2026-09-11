"""
Shared helper: purge stale far-future lines + api predictions before an
ingress run consolidates/predicts.

WHY: the consolidated `betting_lines_consolidated` + raw `betting_lines` rows for a
game scheduled beyond the sport's PREDICT_LOOKAHEAD window are placeholders. The 48h
line-fetch gate only stops NEW writes past the window; it never removed legacy rows that
predate it, and on later runs the (previously cap-less) predictor would happily pick them
up and drop a bogus "pick" for a game 2 weeks out (e.g. MLB game 47857, whose lines were
recorded May-22/Jun-16 and kept surfacing a Top Pick well into Sep).

Each sport ingress calls `purge_farfuture_lines_and_picks(db, schema, predict_days)`
BEFORE fetching new lines. We delete only rows whose game is strictly beyond
`predict_days` from NOW() — i.e. games that could never be predicted this run anyway —
so in-window games are never touched. All deletes are transactional and every deleted row
is first snapshotted into a timestamped backup table for safe rollback.

Only `source='api'` game_predictions are removed. Historical `backtest` predictions are
REAL eval records and are NEVER touched under any circumstance.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger("earl.ingress.purge_farfuture")


async def purge_farfuture_lines_and_picks(db, schema: str, predict_days: int) -> dict:
    """Delete lines + api predictions for games scheduled beyond predict_days.

    Everything guarded to a single transaction; backup table mirrors deleted rows.
    Returns counts: {"predictions_api": n, "consolidated": n, "raw_lines": n}.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = f"{schema}.farfuture_lines_picks_backup_{ts}"
    out = {"predictions_api": 0, "consolidated": 0, "raw_lines": 0}

    async with db.begin():
        # 1) Backup table capturing every row about to be removed, tagged by origin.
        await db.execute(
            text(f"""
                CREATE TABLE {backup} AS
                SELECT 'game_predictions' AS source_table,
                       p.id::text AS row_ref,
                       p.game_id::int AS game_id,
                       null::text AS sportsbook,
                       now() AS purged_at
                FROM {schema}.game_predictions p
                JOIN {schema}.games g ON g.id = p.game_id
                WHERE g.date > now() + make_interval(days => :d)
                  AND p.source = 'api'
                UNION ALL
                SELECT 'consolidated', blc.game_id::text, blc.game_id::int, null, now()
                FROM {schema}.betting_lines_consolidated blc
                JOIN {schema}.games g ON g.id = blc.game_id
                WHERE g.date > now() + make_interval(days => :d)
                UNION ALL
                SELECT 'raw_lines',
                       (bl.game_id::text || ':' || bl.sportsbook),
                       bl.game_id::int, bl.sportsbook, now()
                FROM {schema}.betting_lines bl
                JOIN {schema}.games g ON g.id = bl.game_id
                WHERE g.date > now() + make_interval(days => :d)
            """),
            {"d": predict_days},
        )

        # 2) Delete predictions: ONLY the live 'api' source. backtest records are sacred.
        api_ids = await db.execute(
            text(f"""
                SELECT p.id
                FROM {schema}.game_predictions p
                JOIN {schema}.games g ON g.id = p.game_id
                WHERE g.date > now() + make_interval(days => :d)
                  AND p.source = 'api'
            """),
            {"d": predict_days},
        )
        api_ids = [r[0] for r in api_ids.fetchall()]
        if api_ids:
            await db.execute(
                text(f"""
                    DELETE FROM {schema}.game_predictions p
                    USING {schema}.games g
                    WHERE g.id = p.game_id
                      AND g.date > now() + make_interval(days => :d)
                      AND p.source = 'api'
                """),
                {"d": predict_days},
            )
        out["predictions_api"] = len(api_ids)

        # 3) Delete consolidated line rows for those games.
        con = await db.execute(
            text(f"""
                DELETE FROM {schema}.betting_lines_consolidated blc
                USING {schema}.games g
                WHERE g.id = blc.game_id
                  AND g.date > now() + make_interval(days => :d)
            """),
            {"d": predict_days},
        )
        out["consolidated"] = con.rowcount

        # 4) Delete raw per-sportsbook line rows for those games.
        raw = await db.execute(
            text(f"""
                DELETE FROM {schema}.betting_lines bl
                USING {schema}.games g
                WHERE g.id = bl.game_id
                  AND g.date > now() + make_interval(days => :d)
            """),
            {"d": predict_days},
        )
        out["raw_lines"] = raw.rowcount

    if any(out.values()):
        logger.info(
            f"[{schema}] purged far-future (> {predict_days}d) lines+picks: {out} "
            f"(backup: {backup})"
        )
    else:
        # Drop empty backup table to avoid clutter.
        async with db.begin():
            await db.execute(text(f"DROP TABLE IF EXISTS {backup}"))
    return out
