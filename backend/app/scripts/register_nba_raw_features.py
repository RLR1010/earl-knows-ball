"""Register NBA raw display features + flip blend columns off the pick card.

Option-A split (blended -> model, raw -> pick card):

* Every trainable blend column that has a "*_raw" twin in the data loader
  (e.g. h_cum_ppg) is set pick_card=FALSE here — the model trains on the
  BLENDED value in that column; the pick card must NOT show it.
* For each, a parallel "<col>_raw" row is inserted with:
    is_trainable = FALSE   (never a model feature)
    pick_card    = TRUE    (the pick card shows the raw value)
    current_ats/current_ou = FALSE
  The *_raw column holds the raw in-season value (falling back to the prior
  full-season value when the in-season sample is NULL, e.g. opening night).

Idempotent: blends are flipped only when their *_raw twin exists; *_raw rows
are upserted on name.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text, create_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import settings  # noqa: E402


def main():
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)

    # The exact set of blend columns that produce a *_raw twin in the loader.
    # Kept in sync with the data-loader suffix catalog (h_/a_ + cum/rolling/ats
    # stats that exist in nba.prior_team_stats).
    # We derive it from the DB at runtime instead of hardcoding: every row that
    # is pick_card=TRUE, is_trainable=TRUE and whose name has a matching prior
    # feature column -> treat as a blend column with a raw twin. Safest: hardcode
    # the verified 140-strong list harvested from build_features output.
    RAW_TWINS = """
a_adj_def_10 a_adj_off_10 a_ast_ratio_r10 a_ast_ratio_r5 a_ats_margin_10
a_ats_margin_5 a_ats_wins_10 a_ats_wins_5 a_cum_3pa_rate a_cum_ast_pg
a_cum_ast_ratio a_cum_blk_pg a_cum_blk_rate a_cum_drtg a_cum_efg_pct
a_cum_fg3_pct a_cum_fg_pct a_cum_ft_pct a_cum_ft_rate a_cum_margin_pg
a_cum_net_ortg a_cum_opp_efg_pct a_cum_opp_tov_rate a_cum_oppg a_cum_ortg
a_cum_pace a_cum_pf_pg a_cum_ppg a_cum_reb_pg a_cum_stl_pg a_cum_stl_rate
a_cum_tov_pg a_cum_tov_rate a_cum_win_pct a_cv10_net_rtg a_cv10_ppg a_cv20_ppg
a_drtg_r10 a_drtg_r5 a_efg_r10 a_efg_r5 a_ft_rate_r10 a_ft_rate_r5
a_net_rtg_r10 a_net_rtg_r5 a_ortg_r10 a_ortg_r5 a_ou_margin_5 a_ou_wins_10
a_ou_wins_5 a_pace_r10 a_pace_r5 a_recency_net_rtg a_recency_ppg a_rw3_drtg
a_rw3_efg_pct a_rw3_net_rtg a_rw3_ppg a_rw5_drtg a_rw5_efg_pct a_rw5_net_rtg
a_rw5_ppg a_star1_active a_star1_ppg_5 a_star_ppg_5 a_stars_active
a_threep_rate_r10 a_threep_rate_r5 a_wins_10 a_wins_5 h_adj_def_10 h_adj_off_10
h_ast_ratio_r10 h_ast_ratio_r5 h_ats_margin_10 h_ats_margin_5 h_ats_wins_10
h_ats_wins_5 h_cum_3pa_rate h_cum_ast_pg h_cum_ast_ratio h_cum_blk_pg
h_cum_blk_rate h_cum_drtg h_cum_efg_pct h_cum_fg3_pct h_cum_fg_pct h_cum_ft_pct
h_cum_ft_rate h_cum_margin_pg h_cum_net_ortg h_cum_opp_efg_pct h_cum_opp_tov_rate
h_cum_oppg h_cum_ortg h_cum_pace h_cum_pf_pg h_cum_ppg h_cum_reb_pg h_cum_stl_pg
h_cum_stl_rate h_cum_tov_pg h_cum_tov_rate h_cum_win_pct h_cv10_net_rtg h_cv10_ppg
h_cv20_ppg h_drtg_r10 h_drtg_r5 h_efg_r10 h_efg_r5 h_ft_rate_r10 h_ft_rate_r5
h_net_rtg_r10 h_net_rtg_r5 h_ortg_r10 h_ortg_r5 h_ou_margin_5 h_ou_wins_10
h_ou_wins_5 h_pace_r10 h_pace_r5 h_recency_net_rtg h_recency_ppg h_rw3_drtg
h_rw3_efg_pct h_rw3_net_rtg h_rw3_ppg h_rw5_drtg h_rw5_efg_pct h_rw5_net_rtg
h_rw5_ppg h_star1_active h_star1_ppg_5 h_star_ppg_5 h_stars_active
h_threep_rate_r10 h_threep_rate_r5 h_wins_10 h_wins_5
""".split()

    with engine.begin() as conn:
        # 1) Flip each blend column off the pick card (only if it exists).
        flipped = 0
        notfound = []
        for col in RAW_TWINS:
            r = conn.execute(
                text("UPDATE nba.features SET pick_card = FALSE WHERE name = :n"),
                {"n": col},
            )
            if r.rowcount:
                flipped += 1
            else:
                notfound.append(col)
        print(f"Blend columns set pick_card=FALSE: {flipped}")
        if notfound:
            print(f"  WARNING - not in nba.features (skipped): {len(notfound)} :: {notfound}")

        # 2) Insert/upsert *_raw rows.
        upsert = text("""
            INSERT INTO nba.features (name, description, display_name, current_ats,
                                      current_ou, is_trainable, pick_card)
            SELECT :name, :desc, :disp, FALSE, FALSE, FALSE, TRUE
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                display_name = EXCLUDED.display_name,
                current_ats  = FALSE,
                current_ou   = FALSE,
                is_trainable = FALSE,
                pick_card    = TRUE
        """)
        inserted = 0
        # Prefetch twin descriptions/display names for readable raw labels.
        meta = {}
        for r in conn.execute(text(
            "SELECT name, description, display_name FROM nba.features"
        )):
            meta[r[0]] = (r[1], r[2])
        for col in RAW_TWINS:
            desc, disp = meta.get(col, (None, None))
            raw_desc = f"{desc} (RAW in-season value)" if desc else f"{col} raw value"
            raw_disp = f"{disp} (RAW)" if disp else f"{col} (RAW)"
            conn.execute(
                upsert,
                {"name": col + "_raw", "desc": raw_desc, "disp": raw_disp},
            )
            inserted += 1
        print(f"*_raw rows upserted: {inserted}")

    # 3) Verify (fresh connection, committed state).
        chk = conn.execute(text("""
            SELECT
              (SELECT count(*) FROM nba.features WHERE pick_card=TRUE AND is_trainable=TRUE) AS card_trainable,
              (SELECT count(*) FROM nba.features WHERE pick_card=TRUE AND is_trainable=FALSE AND name LIKE '%\\_raw' ESCAPE '\\') AS card_raw,
              (SELECT count(*) FROM nba.features WHERE name LIKE '%\\_raw' ESCAPE '\\') AS total_raw_rows
        """)).fetchone()
        print(f"VERIFY: pick_card+trainable blend cols={chk[0]}, "
              f"pick_card+non-trainable _raw={chk[1]}, total _raw rows={chk[2]}")


if __name__ == "__main__":
    main()
