"""Public read-only endpoints for prediction results, win rates, profit, and EV.

Mirrors selected admin prediction-stats functionality but without auth —
anyone can see how the picks are performing.

Uses the STORED calibrated confidence and EV scores from game_predictions
rather than recalculating them. Calibrated confidence is treated as the
primary confidence — raw confidence is not shown on the results page.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as _sa_text

from app.database import get_db

router = APIRouter(prefix="/results", tags=["results"])

# ── Helpers ──────────────────────────────────────────────────────────────────

SPORTS = {"nfl", "nba", "mlb"}

def _rl_col(sport: str) -> str:
    """Return the result column for the spread/run-line pick type."""
    return "run_line_result" if sport == "mlb" else "ats_result"


def _ou_is_outcome(sport: str) -> bool:
    """True when the sport stores the OU bet OUTCOME (win/loss) in ou_result.
    All sports (NFL/NBA/MLB) now store the outcome as Win/Loss/Push — the
    older NBA side-convention (over/under) was migrated away in 2026-08-03.
    Kept as a function for forwards-compatibility; always True today."""
    return True


def _ou_win_sql(sport: str) -> str:
    """SQL boolean expression: is an OU prediction a push/win/loss for this sport."""
    if _ou_is_outcome(sport):
        return (
            "LOWER(gp.ou_result) IN ('win','loss')",
            "LOWER(gp.ou_result)='win'",
            "LOWER(gp.ou_result)='loss'",
            "LOWER(gp.ou_result) IN ('push')",
        )
    return (
        "gp.ou_result IS NOT NULL",
        "LOWER(gp.ou_pick) = LOWER(gp.ou_result)",
        "LOWER(gp.ou_pick) <> LOWER(gp.ou_result)",
        "LOWER(gp.ou_result) IN ('push')",
    )


def _ou_is_win(sport: str, pick, result) -> bool:
    """Python helper: is an OU prediction a win for this sport's result convention."""
    if not result:
        return False
    if _ou_is_outcome(sport):
        return str(result).lower() == "win"
    return result and pick and str(pick).lower() == str(result).lower()


def _ou_is_loss(sport: str, pick, result) -> bool:
    """Python helper: is an OU prediction a loss for this sport's result convention."""
    if not result:
        return False
    if _ou_is_outcome(sport):
        return str(result).lower() == "loss"
    return result and pick and str(pick).lower() != str(result).lower()


def _conf_main(sport: str) -> str:
    """Raw confidence column for the spread/run-line pick type."""
    return "rl_conf" if sport == "mlb" else "margin_conf"


def _conf_cols(sport: str) -> str:
    """Raw confidence columns for SELECT (all three)."""
    if sport == "mlb":
        return "gp.rl_conf, gp.rl_conf_cal, gp.ou_conf_cal, gp.ml_conf_cal"
    return "gp.margin_conf as rl_conf, gp.ats_conf_cal, gp.ou_conf_cal, gp.ml_conf_cal"


def _cal_main(sport: str) -> str:
    """Calibrated confidence column for the spread/run-line pick type."""
    return "rl_conf_cal" if sport == "mlb" else "ats_conf_cal"


def _cal_cols(sport: str) -> str:
    """Calibrated confidence columns for SELECT (only the auxiliary ones)."""
    return "gp.ou_conf_cal, gp.ml_conf_cal"


def _ev_cols() -> str:
    """Stored EV columns."""
    return "gp.ats_ev, gp.ou_ev, gp.ml_ev"


def _model_conf_key(sport: str, pick_type: str) -> str:
    """Map a pick_type key (ats/ou/ml) to its calibrated confidence column name."""
    if pick_type == "ats":
        return "rl_conf_cal" if sport == "mlb" else "ats_conf_cal"
    elif pick_type == "ou":
        return "ou_conf_cal"
    elif pick_type == "ml":
        return "ml_conf_cal"
    return "rl_conf_cal"


def _calib_bin_spec(values, bin_count: int = 12, min_games: int = 30):
    """Auto-derive a calibrated-confidence binning that spans the ACTUAL
    observed range of values, per sport per market.

    The old code bucketed into a fixed 0.50 -> 1.00 grid. When a market's
    calibrated confidence lives in a narrow band (e.g. NBA ATS ~0.45-0.57),
    that grid collapsed almost everything into one 0.50 bucket and the chart
    showed no distribution. Instead, snap a padded [min, max] to a clean 0.005
    boundary and build `bin_count` equal-width bins across that actual range.

    To avoid sparse, noisy spike-bins (e.g. 10 games spread across 5 bins), the
    equal-width bins are then merged so that every returned bucket holds at
    least `min_games`. Adjacent low-count bins collapse into their richer
    neighbor, so the chart shows meaningful groups rather than noise fragments.

    Returns (edges, bin_index_fn) where edges is a list of (lo, hi, mid) tuples.
    Handles the degenerate all-equal case by falling back to a 0.05-wide grid
    centred on the value.
    """
    vals = [v for v in values if v is not None and v == v]  # drop None/NaN
    if not vals:
        return [], (lambda cf: -1)

    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.06, 0.005)
    lo = max(0.0, lo - pad)
    hi = min(1.0, hi + pad)

    width = hi - lo
    if width < 0.02:  # all values essentially equal -> 5% grid around the value
        c = (lo + hi) / 2
        lo, hi = max(0.0, c - 0.02), min(1.0, c + 0.02)
        width = hi - lo

    # 1) Build equal-width bin EDGES, keeping the raw boundary list.
    raw_edges = [lo + (width / bin_count) * i for i in range(bin_count + 1)]

    # 2) Count values per raw bin so we can merge sparse ones.
    counts = [0] * bin_count
    for v in vals:
        idx = min(int((v - lo) / width * bin_count), bin_count - 1)
        idx = max(idx, 0)
        counts[idx] += 1

    # 3) Merge adjacent bins that fall below the minimum count. Greedy: repeatedly
    #    fold the sparsest under-filled bin into its neighbor with fewer games.
    total = len(vals)
    floor = max(min_games, int(total * 0.005))
    # edges list of (lo, hi) intervals with their counts
    intervals = [
        (raw_edges[i], raw_edges[i + 1], counts[i]) for i in range(bin_count)
    ]
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(intervals):
            if intervals[i][2] >= floor:
                i += 1
                continue
            # merge with left or right neighbor (whichever is lighter)
            if i > 0 and i + 1 < len(intervals):
                left_n = intervals[i - 1][2]
                right_n = intervals[i + 1][2]
                merge_right = right_n <= left_n
            elif i + 1 < len(intervals):
                merge_right = True
            elif i > 0:
                merge_right = False
            else:
                i += 1
                continue
            if merge_right and i + 1 < len(intervals):
                nxt = intervals[i + 1]
                intervals[i] = (intervals[i][0], nxt[1], intervals[i][2] + nxt[2])
                del intervals[i + 1]
            elif i > 0:
                prv = intervals[i - 1]
                intervals[i - 1] = (prv[0], intervals[i][1], prv[2] + intervals[i][2])
                del intervals[i]
            else:
                i += 1
                continue
            changed = True

    # edge case: merged down to a single interval -> give it a couple of bins
    if len(intervals) == 1:
        lo0, hi0, n0 = intervals[0]
        mid = (lo0 + hi0) / 2
        edges = [
            (round(mid - 0.02, 4), round(mid, 4), round((mid - 0.02 + mid) / 2, 4)),
            (round(mid, 4), round(mid + 0.02, 4), round((mid + mid + 0.02) / 2, 4)),
        ]
    else:
        edges = [
            (round(a, 4), round(b, 4), round((a + b) / 2, 4)) for a, b, _n in intervals
        ]

    _lo, _hi = edges[0][0], edges[-1][1]
    _width = _hi - _lo or 1e-9

    def _bucket_index(cf):
        if cf is None or cf != cf:
            return -1
        if cf < _lo:
            return 0
        if cf > _hi:
            return len(edges) - 1
        # walk edges since they're non-uniform after merging
        for i, (a, b, _m) in enumerate(edges):
            if a <= cf < b or (i == len(edges) - 1 and cf <= b):
                return i
        return len(edges) - 1

    return edges, _bucket_index


def _ev_bin_spec(values, bins_per_side: int = 4):
    """Auto-derive EV buckets that NEVER straddle zero.

    The old grid ran from -100 to +200, so a bucket like [-10, +5] crossed 0 —
    hiding whether plays were genuinely negative- or positive-EV. We instead
    split the observed values at zero: `bins_per_side` equal-width buckets below
    0 (last one ends exactly at 0) and `bins_per_side` above 0 (first one starts
    exactly at 0).

    Returns (edges, bucket_index_fn). edges = list of (lo, hi, label).
    Unknown/zero-EV routes are handled by the caller (exact 0 -> first positive
    bucket by convention; None/NaN -> caller's unknown bucket).
    """
    vals = [v for v in values if v is not None and v == v]
    negs = [v for v in vals if v < 0]
    poss = [v for v in vals if v > 0]

    # ── negative side ──
    neg_edges = []
    neg_start = 0  # where the negative block begins among edges
    if negs:
        lo = min(negs)
        pad = max((max(negs) - lo) * 0.05, 1.0)
        lo = lo - pad
        width = (0 - lo) / bins_per_side
        for i in range(bins_per_side):
            a = lo + width * i
            b = lo + width * (i + 1)
            neg_edges.append((round(a), round(b), f"{round(a)} to {round(b)}"))

    # ── positive side ──
    pos_edges = []
    if poss:
        pos_sorted = sorted(poss)
        n = len(pos_sorted)
        # Place bin boundaries at quantiles of the DATA so the dense 0..+40
        # region (thousands of games) gets broken into several fine buckets. The
        # tail bucket is open-ended over a threshold near the top of the density.
        import math
        # boundaries at these percentiles: [0%, 25%, 50%, 75%, 88%] -> 5 finite
        pcts = [0.0, 0.25, 0.5, 0.75, 0.88]
        bnds = []
        for p in pcts:
            idx = int(round((n - 1) * p))
            bnds.append(pos_sorted[max(0, min(idx, n - 1))])
        # Always force an explicit 0 boundary so the first positive bucket starts
        # at exactly 0 (no gap between the -X to 0 bucket and the first positive).
        bnds = [0.0] + bnds
        # coalesce duplicate/roughly-equal boundaries (keeps 0 first)
        uniq = []
        for v in bnds:
            r = math.floor(v)
            if not uniq:
                uniq.append(r)
            elif r > uniq[-1]:
                uniq.append(r)
        # ensure first bucket boundary is exactly 0
        if not uniq or uniq[0] != 0:
            uniq.insert(0, 0)
        for i, lo_v in enumerate(uniq):
            start = lo_v
            if i == len(uniq) - 1:
                # open-ended tail
                pos_edges.append((start, float("inf"), f"+{start} to ∞"))
            else:
                end = uniq[i + 1]
                if end <= start:
                    continue
                pos_edges.append((start, end, f"+{start} to +{end}"))
        # if nothing was added (all boundaries <=0 or degenerate), fall back to a
        # single open bucket from the max-padding value
        if not pos_edges:
            pos_edges.append((0, float("inf"), "+0 to ∞"))

    edges = neg_edges + pos_edges

    def _bucket_index(ev):
        if ev is None or ev != ev:
            return -1
        # exact zero: land in the first positive bucket
        if ev == 0 and pos_edges:
            return len(neg_edges)
        if ev < 0 and neg_edges:
            idx = int((ev - neg_edges[0][0]) / max((0 - neg_edges[0][0]), 1e-9) * bins_per_side)
            return min(max(idx, 0), len(neg_edges) - 1)
        if ev > 0 and pos_edges:
            # walk the finite positive buckets; anything beyond the last finite
            # upper bound falls into the open-ended ∞ tail bucket.
            for i, (lo_p, hi_p, _lbl) in enumerate(pos_edges):
                if hi_p == float("inf"):
                    return len(neg_edges) + i
                if ev < hi_p:
                    return len(neg_edges) + i
            return len(neg_edges) + len(pos_edges) - 1
        return -1

    return edges, _bucket_index


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/{sport}/yearly")
async def get_results_yearly(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: yearly breakdown of prediction performance, plus EV sum."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)

    rows = await db.execute(_sa_text(f"""
        SELECT
            s.year,
            COUNT(*) FILTER (WHERE gp.{rl_col} IS NOT NULL) as ats_games,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='win') as ats_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='loss') as ats_losses,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col}) IN ('push')) as ats_pushes,
            ROUND(COALESCE(SUM(gp.ats_profit) FILTER (WHERE gp.{rl_col} IS NOT NULL), 0))::int as ats_profit,
            COALESCE(SUM(gp.ats_ev) FILTER (WHERE gp.{rl_col} IS NOT NULL), 0) as ats_ev_sum,
            COUNT(*) FILTER (WHERE gp.ou_result IS NOT NULL) as ou_games,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[1]}) as ou_wins,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[2]}) as ou_losses,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[3]}) as ou_pushes,
            ROUND(COALESCE(SUM(gp.ou_profit) FILTER (WHERE gp.ou_result IS NOT NULL), 0))::int as ou_profit,
            COALESCE(SUM(gp.ou_ev) FILTER (WHERE gp.ou_result IS NOT NULL), 0) as ou_ev_sum,
            COUNT(*) FILTER (WHERE gp.ml_result IS NOT NULL) as ml_games,
            COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='win') as ml_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='loss') as ml_losses,
            ROUND(COALESCE(SUM(gp.ml_profit) FILTER (WHERE gp.ml_result IS NOT NULL), 0))::int as ml_profit,
            COALESCE(SUM(gp.ml_ev) FILTER (WHERE gp.ml_result IS NOT NULL), 0) as ml_ev_sum
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE (gp.{rl_col} IS NOT NULL
           OR gp.ou_result IS NOT NULL
           OR gp.ml_result IS NOT NULL)
          AND s.year != 2021
        GROUP BY s.year
        ORDER BY s.year ASC
    """))

    def _pick(plays, wins, losses, pushes, profit, ev_sum):
        total = wins + losses
        pct = round(wins / total * 100, 1) if total > 0 else 0.0
        return {
            "games": plays,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_pct": pct,
            "profit": profit,
            "fwd_ev_sum": round(ev_sum, 2),
        }

    yearly = []
    for r in rows.fetchall():
        yearly.append({
            "year": r.year,
            "ats": _pick(r.ats_games, r.ats_wins, r.ats_losses, r.ats_pushes, r.ats_profit, r.ats_ev_sum),
            "ou":  _pick(r.ou_games, r.ou_wins, r.ou_losses, r.ou_pushes, r.ou_profit, r.ou_ev_sum),
            "ml":  _pick(r.ml_games, r.ml_wins, r.ml_losses, 0, r.ml_profit, r.ml_ev_sum),
        })

    return {"sport": sport, "yearly": yearly}


@router.get("/{sport}/calibration")
async def get_calibration(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: Calibration buckets — groups predictions by CALIBRATED confidence
    and shows actual win rate per bucket for verification."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    cal_main = _cal_main(sport)
    cal_cols = _cal_cols(sport)

    rows_result = await db.execute(_sa_text(f"""
        SELECT
            gp.{cal_main},
            {cal_cols},
            gp.{rl_col} as ats_result,
            gp.ou_pick,
            gp.ou_result,
            gp.ml_result,
            gp.ats_profit,
            gp.ou_profit,
            gp.ml_profit,
            gp.ats_ev,
            gp.ou_ev,
            gp.ml_ev,
            gp.ats_odds,
            gp.ou_odds,
            gp.ml_odds,
            g.id as game_id
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE gp.{cal_main} IS NOT NULL
    """))

    rows = list(rows_result.fetchall())

    models = {
        "ats": {"conf": cal_main, "result": "ats_result", "profit": "ats_profit", "ev": "ats_ev", "odds": "ats_odds"},
        "ou":  {"conf": "ou_conf_cal", "result": "ou_result", "profit": "ou_profit", "ev": "ou_ev", "odds": "ou_odds"},
        "ml":  {"conf": "ml_conf_cal", "result": "ml_result", "profit": "ml_profit", "ev": "ml_ev", "odds": "ml_odds"},
    }

    # Auto-derive per-market binning from the ACTUAL observed calibrated range
    # (e.g. NBA ATS ~0.45-0.57 becomes 12 equal-width buckets spanning that band), so
    # each chart shows a real distribution instead of everything collapsing to 0.50.
    specs = {}
    for key, m in models.items():
        cf_vals = [getattr(r, m["conf"]) for r in rows]
        specs[key] = _calib_bin_spec(cf_vals)

    def _make_bins(spec):
        return [
            {
                "lo": lo, "hi": hi, "mid": mid,
                "total": 0, "wins": 0, "losses": 0,
                "pct": 0.0,
                "profit": 0.0,
                "fwd_ev_sum": 0.0,
            }
            for lo, hi, mid in spec[0]
        ]

    results: dict[str, list[dict]] = {k: _make_bins(specs[k]) for k in models}
    unknown: dict[str, int] = {k: 0 for k in models}

    for row in rows:
        for key, m in models.items():
            cf = getattr(row, m["conf"])
            _, bucket_index = specs[key]
            idx = bucket_index(cf)
            if idx < 0:
                unknown[key] += 1
                continue

            result_val = getattr(row, m["result"])
            profit_val = getattr(row, m["profit"]) or 0.0
            ev_val = getattr(row, m["ev"]) or 0.0
            b = results[key][idx]

            b["total"] += 1
            if key == "ou":
                ou_pick = getattr(row, "ou_pick", None)
                if _ou_is_win(sport, ou_pick, result_val):
                    b["wins"] += 1
                elif _ou_is_loss(sport, ou_pick, result_val):
                    b["losses"] += 1
            elif result_val and result_val.lower() == "win":
                b["wins"] += 1
            elif result_val and result_val.lower() == "loss":
                b["losses"] += 1
            b["profit"] += profit_val
            b["fwd_ev_sum"] += ev_val

    for key, bins in results.items():
        for b in bins:
            if b["total"] > 0:
                b["pct"] = round(b["wins"] / b["total"], 4)

    return {
        "sport": sport,
        "bins": results,
        "unknown_count": unknown,
    }


@router.get("/{sport}/ev-distribution")
async def get_results_ev_distribution(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: EV distribution — groups predictions by their STORED EV score
    and shows record + profit per EV bucket."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)

    rows_result = await db.execute(_sa_text(f"""
        SELECT
            gp.{rl_col} as ats_result,
            gp.ou_pick,
            gp.ou_result,
            gp.ml_result,
            gp.ats_profit,
            gp.ou_profit,
            gp.ml_profit,
            gp.ats_ev,
            gp.ou_ev,
            gp.ml_ev,
            gp.ats_odds,
            gp.ou_odds,
            gp.ml_odds,
            g.id as game_id
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE gp.ats_ev IS NOT NULL OR gp.ou_ev IS NOT NULL OR gp.ml_ev IS NOT NULL
    """))

    all_rows = list(rows_result.fetchall())

    models = {
        "ats": {"result": "ats_result", "profit": "ats_profit", "ev": "ats_ev", "odds": "ats_odds"},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "ev": "ou_ev", "odds": "ou_odds"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "ev": "ml_ev", "odds": "ml_odds"},
    }

    # ── Auto-derive EV buckets per market, splitting at zero so no bucket
    #    straddles 0 (e.g. -20 to 0 and 0 to +20), spanning the actual range.
    my_ev_specs = {}
    for key, m in models.items():
        ev_vals = []
        for row in all_rows:
            ev_val = getattr(row, m["ev"])
            odds = getattr(row, m["odds"])
            if ev_val is not None and ev_val == ev_val and odds is not None and odds != 0:
                ev_vals.append(ev_val)
        my_ev_specs[key] = _ev_bin_spec(ev_vals)

    def _make_ev_buckets(spec):
        buckets = []
        for lo, hi, label in spec[0]:
            buckets.append({
                "ev_lo": lo, "ev_hi": None if hi == float("inf") else hi,
                "label": label,
                "total": 0, "wins": 0, "losses": 0,
                "profit": 0.0,
            })
        return buckets

    overall_data: dict[str, list[dict]] = {k: _make_ev_buckets(my_ev_specs[k]) for k in models}

    for row in all_rows:
        for key, m in models.items():
            ev_val = getattr(row, m["ev"])
            odds = getattr(row, m["odds"])

            # Drop rows with unknown EV / odds entirely — no Unknown bucket.
            if ev_val is None or odds is None or odds == 0 or ev_val != ev_val:
                continue
            _, ev_idx_fn = my_ev_specs[key]
            idx = ev_idx_fn(ev_val)
            if idx < 0:
                continue
            b = overall_data[key][idx]

            b["total"] += 1

            result_val = None
            if key == "ats":
                result_val = getattr(row, "ats_result")
            elif key == "ou":
                result_val = getattr(row, "ou_result")
            else:
                result_val = getattr(row, "ml_result")

            if key == "ou":
                ou_pick = getattr(row, "ou_pick", None)
                if _ou_is_win(sport, ou_pick, result_val):
                    b["wins"] += 1
                elif _ou_is_loss(sport, ou_pick, result_val):
                    b["losses"] += 1
            elif result_val and result_val.lower() == "win":
                b["wins"] += 1
            elif result_val and result_val.lower() == "loss":
                b["losses"] += 1
            b["profit"] += getattr(row, m["profit"]) or 0

    return {
        "sport": sport,
        "overall": overall_data,
    }


@router.get("/{sport}/ev-distribution-by-year")
async def get_results_ev_distribution_by_year(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: EV distribution by season year — plus overall.

    Groups predictions by their STORED EV score, bucketed into equal ranges.
    """
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)

    rows_result = await db.execute(_sa_text(f"""
        SELECT
            s.year,
            gp.{rl_col} as ats_result,
            gp.ou_pick,
            gp.ou_result,
            gp.ml_result,
            gp.ats_profit,
            gp.ou_profit,
            gp.ml_profit,
            gp.ats_ev,
            gp.ou_ev,
            gp.ml_ev,
            gp.ats_odds,
            gp.ou_odds,
            gp.ml_odds,
            g.id as game_id
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE (gp.ats_ev IS NOT NULL OR gp.ou_ev IS NOT NULL OR gp.ml_ev IS NOT NULL)
          AND s.year != 2021
    """))

    all_rows = list(rows_result.fetchall())

    models = {
        "ats": {"result": "ats_result", "profit": "ats_profit", "ev": "ats_ev", "odds": "ats_odds"},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "ev": "ou_ev", "odds": "ou_odds"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "ev": "ml_ev", "odds": "ml_odds"},
    }

    # ── Auto-derive EV buckets per market, splitting at zero so no bucket
    #    straddles 0. All years + overall share the same edges per market so the
    #    x-axis lines up across years.
    ev_specs = {}
    for key, m in models.items():
        _ev_vals = []
        for _row in all_rows:
            _ev_v = getattr(_row, m["ev"])
            _odds = getattr(_row, m["odds"])
            if _ev_v is not None and _ev_v == _ev_v and _odds is not None and _odds != 0:
                _ev_vals.append(_ev_v)
        ev_specs[key] = _ev_bin_spec(_ev_vals)

    def _make_ev_buckets(spec):
        buckets = []
        for lo, hi, label in spec[0]:
            buckets.append({
                "ev_lo": lo, "ev_hi": None if hi == float("inf") else hi,
                "label": label,
                "total": 0, "wins": 0, "losses": 0,
                "profit": 0.0,
            })
        return buckets

    def _ev_bucket_idx(spec, ev):
        _, idx_fn = spec
        return idx_fn(ev)

    year_data: dict[int, dict[str, list[dict]]] = {}
    overall_data: dict[str, list[dict]] = {k: _make_ev_buckets(ev_specs[k]) for k in models}

    for row in all_rows:
        yr = row.year
        if yr not in year_data:
            year_data[yr] = {k: _make_ev_buckets(ev_specs[k]) for k in models}

        for key, m in models.items():
            ev_val = getattr(row, m["ev"])
            odds = getattr(row, m["odds"])

            # Drop rows with unknown EV / odds entirely — no Unknown bucket.
            if ev_val is None or odds is None or odds == 0 or ev_val != ev_val:
                continue

            eb_idx = _ev_bucket_idx(ev_specs[key], ev_val)
            if eb_idx < 0:
                continue

            for buckets in (year_data[yr][key], overall_data[key]):
                b = buckets[eb_idx]
                b["total"] += 1
                result_val = getattr(row, m["result"])
                if key == "ou":
                    ou_pick = getattr(row, "ou_pick", None)
                    if _ou_is_win(sport, ou_pick, result_val):
                        b["wins"] += 1
                    elif _ou_is_loss(sport, ou_pick, result_val):
                        b["losses"] += 1
                elif result_val and result_val.lower() == "win":
                    b["wins"] += 1
                elif result_val and result_val.lower() == "loss":
                    b["losses"] += 1
                b["profit"] += getattr(row, m["profit"]) or 0

    sorted_years = sorted(year_data.keys(), reverse=True)
    return {
        "sport": sport,
        "overall": overall_data,
        "years": {str(y): year_data[y] for y in sorted_years},
    }


@router.get("/{sport}/summary")
async def get_results_summary(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: overall prediction performance — win rates, profit, ROI by pick type."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)

    rows = await db.execute(_sa_text(f"""
        SELECT
            COUNT(*) FILTER (WHERE gp.{rl_col} IS NOT NULL) as ats_games,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='win') as ats_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='loss') as ats_losses,
            COUNT(*) FILTER (WHERE LOWER(gp.{rl_col}) IN ('push')) as ats_pushes,
            ROUND(COALESCE(SUM(gp.ats_profit) FILTER (WHERE gp.{rl_col} IS NOT NULL), 0))::int as ats_profit,
            COUNT(*) FILTER (WHERE gp.ou_result IS NOT NULL) as ou_games,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[1]}) as ou_wins,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[2]}) as ou_losses,
            COUNT(*) FILTER (WHERE {_ou_win_sql(sport)[3]}) as ou_pushes,
            ROUND(COALESCE(SUM(gp.ou_profit) FILTER (WHERE gp.ou_result IS NOT NULL), 0))::int as ou_profit,
            COUNT(*) FILTER (WHERE gp.ml_result IS NOT NULL) as ml_games,
            COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='win') as ml_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='loss') as ml_losses,
            ROUND(COALESCE(SUM(gp.ml_profit) FILTER (WHERE gp.ml_result IS NOT NULL), 0))::int as ml_profit
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE s.year != 2021
    """))

    r = rows.fetchone()

    def _make_pick_type(plays, wins, losses, pushes, profit):
        total = wins + losses
        roi = round(100 * profit / max(total * 100, 1), 1) if total > 0 else 0.0
        pct = round(wins / total * 100, 1) if total > 0 else 0.0
        return {
            "games": plays,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_pct": pct,
            "profit": profit,
            "roi": roi,
        }

    return {
        "sport": sport,
        "ats": _make_pick_type(r.ats_games, r.ats_wins, r.ats_losses, r.ats_pushes, r.ats_profit),
        "ou": _make_pick_type(r.ou_games, r.ou_wins, r.ou_losses, r.ou_pushes, r.ou_profit),
        "ml": _make_pick_type(r.ml_games, r.ml_wins, r.ml_losses, 0, r.ml_profit),
    }


@router.get("/{sport}/calibration-by-year")
async def get_results_calibration_by_year(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: calibration data broken down by season year — 20 bins per year + overall.

    Uses CALIBRATED confidence to bin predictions, showing actual win rate
    per bin as a calibration check.
    """
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    cal_main = _cal_main(sport)
    cal_cols = _cal_cols(sport)

    rows = await db.execute(_sa_text(f"""
        SELECT
            s.year,
            gp.{cal_main},
            {cal_cols},
            gp.{rl_col} as ats_result, gp.ou_pick, gp.ou_result, gp.ml_result,
            gp.ats_profit, gp.ou_profit, gp.ml_profit,
            gp.ats_odds, gp.ou_odds, gp.ml_odds,
            g.id as game_id
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE (gp.{cal_main} IS NOT NULL
          OR gp.ou_conf_cal IS NOT NULL OR gp.ml_conf_cal IS NOT NULL)
          AND s.year != 2021
    """))

    models = {
        "ats": {"result": "ats_result", "profit": "ats_profit", "odds": "ats_odds", "conf": cal_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf_cal"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf_cal"},
    }

    # Auto-derive per-market bin edges from the ACTUAL observed calibrated range,
    # so each chart spans its real distribution (e.g. NBA ATS ~0.45-0.57) instead of
    # a fixed 0.50-1.00 grid. All years share the same edges so the x-axis lines up.
    all_rows = rows.fetchall()
    bin_specs = {}
    for key, m in models.items():
        cf_vals = [getattr(r, m["conf"]) for r in all_rows]
        bin_specs[key] = _calib_bin_spec(cf_vals)

    def _bucket_index(edges, idx_fn, cf):
        if cf is None or cf != cf:
            return -1
        return idx_fn(cf)

    def _empty_bins(edges):
        bins = []
        for bin_lo, bin_hi, _mid in edges:
            bins.append({
                "bin_lo": bin_lo, "bin_hi": bin_hi,
                "label": f"{bin_lo*100:.0f}-{bin_hi*100:.0f}%",
                "total": 0, "wins": 0, "losses": 0, "pushes": 0,
                "profit": 0.0,
            })
        return bins

    overall_bins: dict[str, list] = {k: _empty_bins(bin_specs[k][0]) for k in models}

    # Accumulate into per-year bins + overall
    year_bins: dict[int, dict[str, list]] = {}

    for row in all_rows:
        yr = row.year
        if yr not in year_bins:
            year_bins[yr] = {k: _empty_bins(bin_specs[k][0]) for k in models}

        for key, m in models.items():
            cf = getattr(row, m["conf"])
            if cf is None or cf != cf:
                continue
            edges, idx_fn = bin_specs[key]
            bidx = _bucket_index(edges, idx_fn, cf)
            if bidx < 0:
                continue
            result = getattr(row, m["result"])
            profit = getattr(row, m["profit"]) or 0

            for bins in (year_bins[yr][key], overall_bins[key]):
                b = bins[bidx]
                b["total"] += 1
                if key == "ou":
                    ou_pick = getattr(row, "ou_pick", None)
                    if _ou_is_win(sport, ou_pick, result):
                        b["wins"] += 1
                    elif _ou_is_loss(sport, ou_pick, result):
                        b["losses"] += 1
                    elif result and result.lower() == "push":
                        b["pushes"] += 1
                elif result and result.lower() == "win":
                    b["wins"] += 1
                elif result and result.lower() == "loss":
                    b["losses"] += 1
                elif result and result.lower() == "push":
                    b["pushes"] += 1
                b["profit"] += profit

    # Sort years descending
    sorted_years = sorted(year_bins.keys(), reverse=True)

    return {
        "sport": sport,
        "overall": overall_bins,
        "years": {str(y): year_bins[y] for y in sorted_years},
    }
