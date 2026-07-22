"""Public read-only endpoints for prediction results, win rates, profit, and EV.

Mirrors selected admin prediction-stats functionality but without auth —
anyone can see how the picks are performing.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as _sa_text

from app.database import get_db

router = APIRouter(prefix="/results", tags=["results"])

# ── Helpers ──────────────────────────────────────────────────────────────────

SPORTS = {"nfl", "nba", "mlb"}

def _schema(sport: str) -> str:
    return sport


def _rl_col(sport: str) -> str:
    return "run_line_result" if sport == "mlb" else "ats_result"


def _conf_main(sport: str) -> str:
    return "rl_conf" if sport == "mlb" else "margin_conf"


def _conf_cols(sport: str) -> str:
    if sport == "mlb":
        return "gp.rl_conf, gp.ml_conf, gp.ou_conf"
    return "gp.margin_conf as rl_conf, gp.ml_conf as ml_conf, gp.ou_conf as ou_conf"


def _profit_per_100(odds: int | None) -> float:
    if odds is None or odds == 0:
        return 0.0
    if odds < 0:
        return 100.0 * 100.0 / float(abs(odds))
    return float(odds)


def _fwd_ev(confidence: float, odds: int | None) -> float:
    if odds is None or confidence is None:
        return 0.0
    p = confidence
    q = 1.0 - p
    win_profit = _profit_per_100(odds)
    return round(p * win_profit - q * 100.0, 2)


# ── Endpoints ────────────────────────────────────────────────────────────────

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


@router.get("/{sport}/yearly")
async def get_results_yearly(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: yearly breakdown of prediction performance."""
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
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        GROUP BY s.year
        ORDER BY s.year DESC
    """))

    yearly = []
    for r in rows.fetchall():
        def _yt(plays, wins, losses, pushes, profit):
            total = wins + losses
            roi = round(100 * profit / max(total * 100, 1), 1) if total > 0 else 0.0
            pct = round(wins / total * 100, 1) if total > 0 else 0.0
            return {"games": plays, "wins": wins, "losses": losses, "pushes": pushes,
                    "win_pct": pct, "profit": profit, "roi": roi}

        yearly.append({
            "year": r.year,
            "ats": _yt(r.ats_games, r.ats_wins, r.ats_losses, r.ats_pushes, r.ats_profit),
            "ou": _yt(r.ou_games, r.ou_wins, r.ou_losses, r.ou_pushes, r.ou_profit),
            "ml": _yt(r.ml_games, r.ml_wins, r.ml_losses, 0, r.ml_profit),
        })

    return {"sport": sport, "yearly": yearly}


@router.get("/{sport}/calibration")
async def get_results_calibration(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: calibration data — 20 confidence bins (50%-100%) with win rates."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    conf_main = _conf_main(sport)
    conf_cols = _conf_cols(sport)

    rows = await db.execute(_sa_text(f"""
        SELECT
            gp.{conf_main},
            {conf_cols},
            gp.{rl_col} as ats_result, gp.ou_result, gp.ml_result,
            gp.ats_profit, gp.ou_profit, gp.ml_profit,
            gp.ats_odds, gp.ou_odds, gp.ml_odds
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE gp.{conf_main} IS NOT NULL
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
                "profit": 0.0, "fwd_ev_sum": 0.0, "odds_sum": 0.0,
            })
        return bins

    # Map model → result/odds/profit column names
    models = {
        "ats": {"result": rl_col, "profit": "ats_profit", "odds": "ats_odds", "conf": conf_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf"},
    }

    cal = {}
    for key, m in models.items():
        bins = _empty_bins()
        for row in rows.fetchall():
            cf = getattr(row, m["conf"])
            if cf is None or cf != cf:
                continue
            bidx = _bucket_index(cf)
            result = getattr(row, f"{key}_result") if key == "ats" else getattr(row, m["result"])
            b = bins[bidx]
            b["total"] += 1
            if result and result.lower() == "win":
                b["wins"] += 1
            elif result and result.lower() == "loss":
                b["losses"] += 1
            elif result and result.lower() == "push":
                b["pushes"] += 1
            profit = getattr(row, m["profit"]) or 0
            b["profit"] += profit
            odds = getattr(row, m["odds"])
            b["odds_sum"] += (odds or 0)
            if cf and odds:
                b["fwd_ev_sum"] += _fwd_ev(cf, odds)

        cal[key] = bins

    return {"sport": sport, "calibration": cal}


@router.get("/{sport}/calibration-by-year")
async def get_results_calibration_by_year(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: calibration data broken down by season year — 20 bins per year + overall."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    conf_main = _conf_main(sport)
    conf_cols = _conf_cols(sport)

    rows = await db.execute(_sa_text(f"""
        SELECT
            s.year,
            gp.{conf_main},
            {conf_cols},
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
        WHERE gp.{conf_main} IS NOT NULL
          OR gp.ou_conf IS NOT NULL OR gp.ml_conf IS NOT NULL
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
        "ats": {"result": rl_col, "profit": "ats_profit", "odds": "ats_odds", "conf": conf_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf"},
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
            result = getattr(row, f"{key}_result") if key in ("ats",) else getattr(row, m["result"])
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


@router.get("/{sport}/ev-distribution")
async def get_results_ev_distribution(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: EV distribution — using CALIBRATED confidence (observed win rate)
    rather than raw model confidence to calculate EV scores."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    conf_main = _conf_main(sport)
    conf_cols = _conf_cols(sport)

    rows_result = await db.execute(_sa_text(f"""
        SELECT
            gp.{conf_main},
            {conf_cols},
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
        WHERE gp.{conf_main} IS NOT NULL OR gp.ou_conf IS NOT NULL OR gp.ml_conf IS NOT NULL
    """))

    all_rows = list(rows_result.fetchall())

    models = {
        "ats": {"result": rl_col, "profit": "ats_profit", "odds": "ats_odds", "conf": conf_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf"},
    }

    # ── Step 1: Compute calibration buckets to get calibrated confidence for each raw bin ──
    BIN_COUNT = 20
    BIN_STEP = 0.025

    def _bucket_index(cf: float) -> int:
        if cf is None or cf != cf or cf < 0.50:
            return 0
        if cf >= 1.0:
            return BIN_COUNT - 1
        idx = int((cf - 0.50) / BIN_STEP)
        return min(idx, BIN_COUNT - 1)

    # ── Running calibration: update on the fly per row (matches admin page) ──
    # EV buckets: 20 buckets spanning from -$100 to +$200 in $15 increments
    ev_buckets = []
    for i in range(20):
        lo = -100 + i * 15
        hi = lo + 15
        ev_buckets.append({
            "ev_lo": lo, "ev_hi": hi,
            "label": f"{lo}-{hi}",
            "total": 0, "wins": 0, "losses": 0,
            "profit": 0.0,
        })
    # Extra bucket for rows with no valid odds (lines unavailable at prediction time)
    ev_buckets.append({
        "ev_lo": 201, "ev_hi": 999,
        "label": "Unknown odds",
        "total": 0, "wins": 0, "losses": 0,
        "profit": 0.0,
    })
    UNKNOWN_BUCKET = 20

    def _ev_bucket_idx(ev: float) -> int:
        if ev < -100:
            return 0
        if ev >= -100 + 20 * 15:
            return 19
        idx = int((ev + 100) / 15)
        return min(idx, 19)

    # Initialize running calibration counters
    calibrations: dict[str, list[dict]] = {
        k: [{"w": 0, "l": 0} for _ in range(BIN_COUNT)] for k in models
    }

    result = {}
    for key, m in models.items():
        buckets = [dict(b) for b in ev_buckets]
        for row in all_rows:
            cf = getattr(row, m["conf"])
            odds = getattr(row, m["odds"])

            # Rows with None/NaN confidence or odds=None/odds=0 can't have a meaningful
            # EV computed, but their profit must still be counted so totals match.
            # Route them to the "Unknown odds" bucket.
            if cf is None or cf != cf or odds is None or odds == 0:
                b = buckets[UNKNOWN_BUCKET]
                b["total"] += 1
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
                profit = getattr(row, m["profit"]) or 0
                b["profit"] += profit
                continue

            # Use calibrated confidence instead of raw
            # but their profit must still be counted so totals match admin/predictions.
            # Route them to the "Unknown odds" bucket.
            if odds is None or odds == 0:
                b = buckets[UNKNOWN_BUCKET]
                b["total"] += 1
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
                profit = getattr(row, m["profit"]) or 0
                b["profit"] += profit
                continue

            # ── Running calibration (matches admin page) ──
            bidx = _bucket_index(cf)

            # Update calibration WITH this row's result FIRST
            if key == "ats":
                result_val = getattr(row, "ats_result")
            elif key == "ou":
                result_val = getattr(row, "ou_result")
            else:
                result_val = getattr(row, "ml_result")
            cal = calibrations[key][bidx]
            if result_val and result_val.lower() == "win":
                cal["w"] += 1
            elif result_val and result_val.lower() == "loss":
                cal["l"] += 1

            # Compute EV using the updated calibration
            total = cal["w"] + cal["l"]
            wr = cal["w"] / total if total > 0 else 0.50
            profit_amt = _profit_per_100(odds)
            ev = wr * profit_amt - (1.0 - wr) * 100.0

            eb_idx = _ev_bucket_idx(ev)
            b = buckets[eb_idx]
            b["total"] += 1

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
            profit = getattr(row, m["profit"]) or 0
            b["profit"] += profit

        result[key] = buckets

    return {"sport": sport, "ev": result}


@router.get("/{sport}/ev-distribution-by-year")
async def get_results_ev_distribution_by_year(
    sport: str,
    db: AsyncSession = Depends(get_db),
):
    """Public: EV distribution by season year — plus overall."""
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(404, f"Unknown sport: {sport}")

    schema = sport
    rl_col = _rl_col(sport)
    conf_main = _conf_main(sport)
    conf_cols = _conf_cols(sport)

    rows_result = await db.execute(_sa_text(f"""
        SELECT
            s.year,
            gp.{conf_main},
            {conf_cols},
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
        WHERE gp.{conf_main} IS NOT NULL OR gp.ou_conf IS NOT NULL OR gp.ml_conf IS NOT NULL
    """))

    all_rows = list(rows_result.fetchall())

    models = {
        "ats": {"result": rl_col, "profit": "ats_profit", "odds": "ats_odds", "conf": conf_main},
        "ou":  {"result": "ou_result", "profit": "ou_profit", "odds": "ou_odds", "conf": "ou_conf"},
        "ml":  {"result": "ml_result", "profit": "ml_profit", "odds": "ml_odds", "conf": "ml_conf"},
    }

    BIN_COUNT = 20
    BIN_STEP = 0.025

    def _bucket_index(cf: float) -> int:
        if cf is None or cf != cf or cf < 0.50:
            return 0
        if cf >= 1.0:
            return BIN_COUNT - 1
        idx = int((cf - 0.50) / BIN_STEP)
        return min(idx, BIN_COUNT - 1)

    # ── EV buckets ──
    UNKNOWN_BUCKET = 20

    def _make_ev_buckets():
        buckets = []
        for i in range(20):
            lo = -100 + i * 15
            hi = lo + 15
            buckets.append({
                "ev_lo": lo, "ev_hi": hi,
                "label": f"{lo}-{hi}",
                "total": 0, "wins": 0, "losses": 0,
                "profit": 0.0,
            })
        # Extra bucket for rows with odds=0 (lines unavailable at prediction time)
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
        if ev >= -100 + 20 * 15:
            return 19
        idx = int((ev + 100) / 15)
        return min(idx, 19)

    # ── Pass 2: bucket into overall + per-year ──
    # Initialize running calibration counters
    calibrations: dict[str, list[dict]] = {
        k: [{"w": 0, "l": 0} for _ in range(BIN_COUNT)] for k in models
    }

    year_data: dict[int, dict[str, list[dict]]] = {}
    overall_data: dict[str, list[dict]] = {k: _make_ev_buckets() for k in models}

    for row in all_rows:
        yr = row.year
        if yr not in year_data:
            year_data[yr] = {k: _make_ev_buckets() for k in models}

        for key, m in models.items():
            cf = getattr(row, m["conf"])
            odds = getattr(row, m["odds"])
            if cf is None or cf != cf or odds is None or odds == 0:
                # Can't compute EV without valid odds; route to Unknown odds bucket.
                for buckets in (year_data[yr][key], overall_data[key]):
                    b = buckets[UNKNOWN_BUCKET]
                    b["total"] += 1
                    if key == "ats":
                        rv = getattr(row, "ats_result")
                    elif key == "ou":
                        rv = getattr(row, "ou_result")
                    else:
                        rv = getattr(row, "ml_result")
                    if rv and rv.lower() == "win":
                        b["wins"] += 1
                    elif rv and rv.lower() == "loss":
                        b["losses"] += 1
                    b["profit"] += getattr(row, m["profit"]) or 0
                continue

            # ── Running calibration (matches admin page) ──
            bidx = _bucket_index(cf)

            # Update calibration WITH this row's result FIRST
            if key == "ats":
                rv = getattr(row, "ats_result")
            elif key == "ou":
                rv = getattr(row, "ou_result")
            else:
                rv = getattr(row, "ml_result")
            cal = calibrations[key][bidx]
            if rv and rv.lower() == "win":
                cal["w"] += 1
            elif rv and rv.lower() == "loss":
                cal["l"] += 1

            # Compute EV using the updated calibration
            total = cal["w"] + cal["l"]
            wr = cal["w"] / total if total > 0 else 0.50
            profit_amt = _profit_per_100(odds)
            ev_val = wr * profit_amt - (1.0 - wr) * 100.0

            eb_idx = _ev_bucket_idx(ev_val)

            for buckets in (year_data[yr][key], overall_data[key]):
                b = buckets[eb_idx]
                b["total"] += 1
                if key == "ats":
                    rv = getattr(row, "ats_result")
                elif key == "ou":
                    rv = getattr(row, "ou_result")
                else:
                    rv = getattr(row, "ml_result")
                if rv and rv.lower() == "win":
                    b["wins"] += 1
                elif rv and rv.lower() == "loss":
                    b["losses"] += 1
                b["profit"] += getattr(row, m["profit"]) or 0

    sorted_years = sorted(year_data.keys(), reverse=True)
    return {
        "sport": sport,
        "overall": overall_data,
        "years": {str(y): year_data[y] for y in sorted_years},
    }
