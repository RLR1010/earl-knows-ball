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
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='win') as ou_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='loss') as ou_losses,
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result) IN ('push')) as ou_pushes,
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
        WHERE gp.{rl_col} IS NOT NULL
           OR gp.ou_result IS NOT NULL
           OR gp.ml_result IS NOT NULL
        GROUP BY s.year
        ORDER BY s.year DESC
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

    BIN_COUNT = 20
    BIN_STEP = 0.025

    def _make_bins():
        return [
            {
                "lo": round(0.50 + i * BIN_STEP, 3),
                "hi": round(0.50 + (i + 1) * BIN_STEP, 3),
                "mid": round(0.50 + (i + 0.5) * BIN_STEP, 3),
                "total": 0, "wins": 0, "losses": 0,
                "pct": 0.0,
                "profit": 0.0,
                "fwd_ev_sum": 0.0,
            }
            for i in range(BIN_COUNT)
        ]

    def _bucket_index(cf: float) -> int:
        if cf is None or cf != cf or cf < 0.50:
            return -1
        if cf >= 1.0:
            return BIN_COUNT - 1
        idx = int((cf - 0.50) / BIN_STEP)
        return min(idx, BIN_COUNT - 1)

    results: dict[str, list[dict]] = {k: _make_bins() for k in models}
    unknown: dict[str, int] = {k: 0 for k in models}

    for row in rows:
        for key, m in models.items():
            cf = getattr(row, m["conf"])
            idx = _bucket_index(cf)
            if idx < 0:
                unknown[key] += 1
                continue

            result_val = getattr(row, m["result"])
            profit_val = getattr(row, m["profit"]) or 0.0
            ev_val = getattr(row, m["ev"]) or 0.0
            b = results[key][idx]

            b["total"] += 1
            if result_val and result_val.lower() == "win":
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

    # ── EV buckets: -100 to +200, 15-unit steps ──
    EV_BUCKET_WIDTH = 15
    EV_NUM_BUCKETS = 20
    UNKNOWN_ODDS_BUCKET = 20

    def _make_ev_buckets():
        buckets = []
        for i in range(EV_NUM_BUCKETS):
            lo = -100 + i * EV_BUCKET_WIDTH
            hi = lo + EV_BUCKET_WIDTH
            buckets.append({
                "ev_lo": lo, "ev_hi": hi,
                "label": f"{lo}-{hi}",
                "total": 0, "wins": 0, "losses": 0,
                "profit": 0.0,
            })
        # Extra bucket for rows where EV couldn't be computed (odds=0 at prediction time)
        buckets.append({
            "ev_lo": 201, "ev_hi": 999,
            "label": "Unknown odds",
            "total": 0, "wins": 0, "losses": 0,
            "profit": 0.0,
        })
        return buckets

    def _ev_bucket_idx(ev: float) -> int:
        if ev < -100:
            return 0
        idx = int((ev + 100) / EV_BUCKET_WIDTH)
        if idx >= EV_NUM_BUCKETS:
            return EV_NUM_BUCKETS - 1
        return idx

    overall_data: dict[str, list[dict]] = {k: _make_ev_buckets() for k in models}

    for row in all_rows:
        for key, m in models.items():
            ev_val = getattr(row, m["ev"])
            odds = getattr(row, m["odds"])

            if ev_val is None or odds is None or odds == 0:
                b = overall_data[key][UNKNOWN_ODDS_BUCKET]
            else:
                b = overall_data[key][_ev_bucket_idx(ev_val)]

            b["total"] += 1

            result_val = None
            if key == "ats":
                result_val = getattr(row, "ats_result")
            elif key == "ou":
                result_val = getattr(row, "ou_result")
            else:
                result_val = getattr(row, "ml_result")

            if result_val and result_val.lower() == "win":
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

    # ── EV buckets ──
    EV_BUCKET_WIDTH = 15
    EV_NUM_BUCKETS = 20
    UNKNOWN_ODDS_BUCKET = 20

    def _make_ev_buckets():
        buckets = []
        for i in range(EV_NUM_BUCKETS):
            lo = -100 + i * EV_BUCKET_WIDTH
            hi = lo + EV_BUCKET_WIDTH
            buckets.append({
                "ev_lo": lo, "ev_hi": hi,
                "label": f"{lo}-{hi}",
                "total": 0, "wins": 0, "losses": 0,
                "profit": 0.0,
            })
        buckets.append({
            "ev_lo": 201, "ev_hi": 999,
            "label": "Unknown odds",
            "total": 0, "wins": 0, "losses": 0,
            "profit": 0.0,
        })
        return buckets

    def _ev_bucket_idx(ev: float) -> int:
        if ev < -100:
            return 0
        idx = int((ev + 100) / EV_BUCKET_WIDTH)
        if idx >= EV_NUM_BUCKETS:
            return EV_NUM_BUCKETS - 1
        return idx

    year_data: dict[int, dict[str, list[dict]]] = {}
    overall_data: dict[str, list[dict]] = {k: _make_ev_buckets() for k in models}

    for row in all_rows:
        yr = row.year
        if yr not in year_data:
            year_data[yr] = {k: _make_ev_buckets() for k in models}

        for key, m in models.items():
            ev_val = getattr(row, m["ev"])
            odds = getattr(row, m["odds"])

            if ev_val is None or odds is None or odds == 0:
                for buckets in (year_data[yr][key], overall_data[key]):
                    b = buckets[UNKNOWN_ODDS_BUCKET]
                    b["total"] += 1
                    result_val = getattr(row, m["result"])
                    if result_val and result_val.lower() == "win":
                        b["wins"] += 1
                    elif result_val and result_val.lower() == "loss":
                        b["losses"] += 1
                    b["profit"] += getattr(row, m["profit"]) or 0
                continue

            eb_idx = _ev_bucket_idx(ev_val)

            for buckets in (year_data[yr][key], overall_data[key]):
                b = buckets[eb_idx]
                b["total"] += 1
                result_val = getattr(row, m["result"])
                if result_val and result_val.lower() == "win":
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
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='win') as ou_wins,
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='loss') as ou_losses,
            COUNT(*) FILTER (WHERE LOWER(gp.ou_result) IN ('push')) as ou_pushes,
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
            gp.{rl_col} as ats_result, gp.ou_result, gp.ml_result,
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
        WHERE gp.{cal_main} IS NOT NULL
          OR gp.ou_conf_cal IS NOT NULL OR gp.ml_conf_cal IS NOT NULL
    """))

    BIN_COUNT = 20
    BIN_STEP = 0.025

    def _bucket_index(cf: float) -> int:
        if cf is None or cf != cf or cf < 0.50:
            return 0
        if cf >= 1.0:
            return BIN_COUNT - 1
        idx = int((cf - 0.50) / BIN_STEP)
        return min(idx, BIN_COUNT - 1)

    def _empty_bins():
        bins = []
        for i in range(BIN_COUNT):
            lo = round(0.50 + i * BIN_STEP, 3)
            hi = round(lo + BIN_STEP, 3)
            bins.append({
                "bin_lo": lo, "bin_hi": hi,
                "label": f"{lo*100:.0f}-{hi*100:.0f}%",
                "total": 0, "wins": 0, "losses": 0, "pushes": 0,
                "profit": 0.0,
            })
        return bins

    models = {
        "ats": {"result": "ats_result", "profit": "ats_profit", "odds": "ats_odds", "conf": cal_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf_cal"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf_cal"},
    }

    # Accumulate into per-year bins + overall
    all_rows = rows.fetchall()
    year_bins: dict[int, dict[str, list]] = {}
    overall_bins: dict[str, list] = {k: _empty_bins() for k in models}

    for row in all_rows:
        yr = row.year
        if yr not in year_bins:
            year_bins[yr] = {k: _empty_bins() for k in models}

        for key, m in models.items():
            cf = getattr(row, m["conf"])
            if cf is None or cf != cf:
                continue
            bidx = _bucket_index(cf)
            result = getattr(row, m["result"])
            profit = getattr(row, m["profit"]) or 0

            for bins in (year_bins[yr][key], overall_bins[key]):
                b = bins[bidx]
                b["total"] += 1
                if result and result.lower() == "win":
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
