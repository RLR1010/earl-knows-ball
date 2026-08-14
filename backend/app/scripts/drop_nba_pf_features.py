"""Drop the two dead personal-foul features (h_cum_pf_pg / a_cum_pf_pg) from the
NBA trainable feature set. home_fouls/away_fouls are NULL in 100% of games in every
season (verified in NBA_STATS_AUDIT), so COALESCE(fouls,0) makes these constant-zero
with zero predictive signal. We set is_trainable=FALSE (keep the columns for display).
Runs in a single engine.begin() transaction; verified from a fresh connection.
"""
from sqlalchemy import create_engine, text

ENGINE = create_engine('postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football')
TARGETS = ['h_cum_pf_pg', 'a_cum_pf_pg']

with ENGINE.begin() as conn:
    for name in TARGETS:
        res = conn.execute(
            text("UPDATE nba.features SET is_trainable = FALSE WHERE name = :n"),
            {'n': name},
        )
        print(f"updated {name}: {res.rowcount} row(s)")
print("committed (engine.begin)")

# Verify from a FRESH connection (avoids reading the same uncommitted session)
with ENGINE.connect() as conn:
    for name in TARGETS:
        r = conn.execute(
            text("SELECT name, is_trainable, current_ats, current_ou FROM nba.features WHERE name = :n"),
            {'n': name},
        ).fetchone()
        print(f"FRESH verify {r[0]}: is_trainable={r[1]} ats={r[2]} ou={r[3]}")
