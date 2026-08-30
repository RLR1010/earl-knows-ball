"""
_query_guard — result-size guards for the MLB/NFL/NBA query engines.

Why: a league-wide leaderboard query (group_by=['player'], no `top`) returns the
entire roster (thousands of rows). The engine sends the full serialized result back
through the LLM tool result, which (a) inflates the context window on every
subsequent turn, and (b) bills the user for those input tokens repeatedly. Capping
the row count bounds both the DB load and the token-compounding blast radius without
hurting typical leaderboard answer quality.

Design (Rich, 2026-08-28):
  * DEFAULT_TOP (500): the row cap applied when the model omits `top`, OR when it
    requests more than DEFAULT_TOP. A league-wide leaderboard returns at most this
    many rows, so no "all players" query ever floods context by default.
  * HARD_TOP (2000): the absolute ceiling. An explicit `top` up to HARD_TOP is
    honored (the escape hatch for legit deep dives — e.g. a full league dump the
    model genuinely needs). Values above HARD_TOP are clamped to HARD_TOP and a note
    is returned so the model knows more rows exist and can page/target with filters.
  * Both constants live here so the three engines share one tuning knob.
"""
from __future__ import annotations

from sqlalchemy import text

DEFAULT_TOP = 500   # default row cap when `top` omitted or over-requests
HARD_TOP = 2000     # absolute ceiling; escape hatch for explicit deep-dive requests


def count_note(limit, returned_rows, true_total):
    """Return a truncation note only when rows were actually cut off at the LIMIT
    ceiling. `returned_rows` is len(results) after LIMIT; `true_total` is the true
    matching-row count from a parallel COUNT(*). When they differ, the result was
    truncated and the model should be told so it can narrow with filters/order.
    Returns None when nothing was truncated (no note cluttering clean results)."""
    if limit is None or true_total is None:
        return None
    if returned_rows < true_total:
        return f"truncated to {limit} rows ({true_total} match total); add filters to narrow"
    return None


def resolve_top(top):
    """Resolve a requested `top` into (limit, clamped, note).

    Returns (limit, clamped_bool, note_or_None):

    * top omitted (None)                 -> (DEFAULT_TOP, False, None)
    * 1 <= top <= HARD_TOP               -> (top,          False, None)
    * top > HARD_TOP                     -> (HARD_TOP,     True, "clamped to HARD_TOP; use filters to narrow")

    `limit is None` signals an invalid `top` (non-integer / < 1); callers should
    surface the error rather than proceeding with an un-clamped query.
    """
    if top is None:
        return DEFAULT_TOP, False, None
    try:
        n = int(top)
    except (TypeError, ValueError):
        return None, True, None
    if n < 1:
        return None, True, None
    if n > HARD_TOP:
        return HARD_TOP, True, f"clamped to {HARD_TOP} (requested {n}); add filters to narrow"
    return n, False, None


def apply_limit(sql, top):
    """Append a bounded LIMIT clause to `sql` using the resolve_top rules.

    Always bounds the result (DEFAULT_TOP when `top` omitted; HARD_TOP escape hatch
    when the model explicitly over-requests).

    Returns (sql_or_None, limit_or_error):
      * valid top   -> (sql with LIMIT appended, the limit integer used)
      * invalid top -> (None, "'top' must be a positive integer" error string)
    Callers pass the returned limit to count_note(limit, len(rows), true_total) to
    build the truncation note from the ACTUAL full-match count.
    """
    limit, _clamped, _note = resolve_top(top)
    if limit is None:
        return None, "'top' must be a positive integer"
    return f"{sql} LIMIT {limit}", limit


def to_count_sql(sql):
    """Derive an accurate `SELECT COUNT(*)` over the same rows as `sql` (which has
    a trailing LIMIT and possibly GROUP BY/ORDER BY). Used to compute the true
    matching row count for a truncation note.

    * Plain (no GROUP BY): SELECT COUNT(*) over the same FROM/WHERE.
    * Grouped:             SELECT COUNT(*) over (SELECT 1 FROM ... GROUP BY ...)
      so it counts the number of GROUPS (e.g. distinct players), not underlying
      rows. ORDER BY and LIMIT are dropped entirely (we want the full total).

    Returns None on any unexpected shape (caller then falls back to no note rather
    than emitting a wrong count).
    """
    try:
        base = sql.split(" LIMIT ")[0]  # drop trailing LIMIT
        # drop trailing ORDER BY (if any)
        ob = base.upper().rfind(" ORDER BY ")
        if ob != -1:
            base = base[:ob]
        fi = base.upper().find(" FROM ")
        if fi == -1:
            return None
        gb = base.upper().rfind(" GROUP BY ")
        if gb != -1 and " group by " in base.lower():
            # count groups: wrap the grouped select in a subquery
            return "SELECT COUNT(*) FROM (SELECT 1" + base[fi:] + ") g"
        # plain: project COUNT(*) over the same FROM/WHERE
        return "SELECT COUNT(*) " + base[fi:]
    except Exception:
        return None


async def async_count(db, sql_with_limit, params=None):
    """Return the true matching-row count for `sql_with_limit` (a built query with
    a trailing LIMIT) by running a parallel COUNT(*) over the same FROM/WHERE/GROUP.
    Returns an int, or None if the count SQL could not be derived or the query
    failed (caller then omits the note rather than risking a wrong count)."""
    count_sql = to_count_sql(sql_with_limit)
    if not count_sql:
        return None
    try:
        r = await db.execute(text(count_sql), params or {})
        row = r.first()
        return int(row[0]) if row is not None else None
    except Exception:
        return None
