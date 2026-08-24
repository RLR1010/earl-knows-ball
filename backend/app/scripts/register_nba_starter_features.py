"""Register NBA starter-only active-roster features in nba.features.

Mirrors the existing active-roster feature flags exactly (is_trainable /
current_ats / current_ou / pick_card / description / display_name).

Idempotent: INSERT ... ON CONFLICT (name) DO UPDATE.
"""
from psycopg2 import connect, extras

DB = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

# (name, display, description, is_trainable, current_ats, current_ou, pick_card)
FEATURES = [
    ("h_starter_pts", "Home Starter PTS", "Sum of home starters' season-to-date PPG (5-man).", True, False, True, True),
    ("h_starter_reb", "Home Starter REB", "Sum of home starters' season-to-date RPG (5-man).", True, False, True, True),
    ("h_starter_ast", "Home Starter AST", "Sum of home starters' season-to-date APG (5-man).", True, False, True, True),
    ("h_starter_n", "Home Starter Count", "Number of home starters with season-to-date stats.", False, False, False, False),
    ("h_starter_pts_minus_team", "Home Starter PTS vs Team Avg", "Home starters' PPG sum vs team season PPG.", True, True, True, True),
    ("h_starter_reb_minus_team", "Home Starter REB vs Team Avg", "Home starters' RPG sum vs team season RPG.", True, True, True, True),
    ("h_starter_ast_minus_team", "Home Starter AST vs Team Avg", "Home starters' APG sum vs team season APG.", True, True, True, True),
    ("a_starter_pts", "Away Starter PTS", "Sum of away starters' season-to-date PPG (5-man).", True, False, True, True),
    ("a_starter_reb", "Away Starter REB", "Sum of away starters' season-to-date RPG (5-man).", True, False, True, True),
    ("a_starter_ast", "Away Starter AST", "Sum of away starters' season-to-date APG (5-man).", True, False, True, True),
    ("a_starter_n", "Away Starter Count", "Number of away starters with season-to-date stats.", False, False, False, False),
    ("a_starter_pts_minus_team", "Away Starter PTS vs Team Avg", "Away starters' PPG sum vs team season PPG.", True, True, True, True),
    ("a_starter_reb_minus_team", "Away Starter REB vs Team Avg", "Away starters' RPG sum vs team season RPG.", True, True, True, True),
    ("a_starter_ast_minus_team", "Away Starter AST vs Team Avg", "Away starters' APG sum vs team season APG.", True, True, True, True),
]

SQL = """
INSERT INTO nba.features
    (name, display_name, description, is_trainable, current_ats, current_ou,
     live_ats, live_ou, pick_card, created_at)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    is_trainable = EXCLUDED.is_trainable,
    current_ats = EXCLUDED.current_ats,
    current_ou = EXCLUDED.current_ou,
    live_ats = EXCLUDED.live_ats,
    live_ou = EXCLUDED.live_ou,
    pick_card = EXCLUDED.pick_card
"""

with connect(DB) as conn:
    with conn.cursor() as cur:
        for name, disp, desc, tr, ats, ou, pc in FEATURES:
            cur.execute(SQL, (name, disp, desc, tr, ats, ou, ats, ou, pc))
            print("registered", name)
    conn.commit()
print("done")
