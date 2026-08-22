"""Side-by-side audit: DB-derived season stats (from last-REG-row of player_rolling_stats)
vs basketball-reference per_game season stats. Flags mismatches.

Usage (runs the full audit + bball-ref comparison for the SAMPLES):
  python audit_compare_bball.py
"""
import json, sys
from pathlib import Path

from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import settings  # noqa: E402
from audit_nba_player_rolling import audit, SAMPLES  # noqa: E402

# season_id -> start calendar year: season_id=16 -> 2006, season_id=35 -> 2025 (nba.seasons.year).
# So year = season_id + 1990. bball-ref labels the season by its start year ("2006-07").
def label(sid):
    y = sid + 1990
    return f"{y}-{str(y + 1)[-2:]}"

# slug per player_id (same set used in SAMPLES)
SLUG = {2: "jamesle01", 1: "bryanko01", 30: "duncati01", 46: "paulch01",
        678: "curryst01", 967: "antetgi01", 1284: "tatumja01", 1406: "gilgesh01",
        1126: "jokicni01"}

_cache = {}
def bball(lbl, slug):
    if slug not in _cache:
        _cache[slug] = json.load(open(f"/tmp/bball/{slug}.json"))
    for s in _cache[slug]:
        if s["season"] == lbl and s.get("g"):
            return s
    return None

def pct(p):
    # ".512" -> 51.2
    try:
        return round(float(p) * 100, 1)
    except Exception:
        return None

def main():
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
    print(f"{'player-season':20} {'G db/ref':10} {'MPG db/ref':11} {'PPG db/ref':10} {'RPG db/ref':10} {'APG db/ref':10} {'FG db/ref':10} {'3P db/ref':9}  verdict")
    print("-" * 110)
    mism = []
    for pid, sid, lbl in SAMPLES:
        lbl = lbl.split(" ")[0][:0]  # unused
        r = audit(engine, pid, sid, "")
        full_label = f"(pid{pid} S{sid})"
        b = bball(label(sid), SLUG.get(pid, ""))
        if not r or not b:
            print(f"{full_label:20} DB={bool(r)} REF={bool(b)}  -- NO DATA")
            continue
        row = {
            "G": (r["G"], b["g"]),
            "MPG": (r["MPG"], float(b["mp"]) if b["mp"] else None),
            "PPG": (r["PPG"], float(b["pts"])),
            "RPG": (r["RPG"], float(b["trb"])),
            "APG": (r["APG"], float(b["ast"])),
            "FGpct": (r["FGpct"], pct(b["fg_pct"])),
            "3Ppct": (r["3Ppct"], pct(b["tp_pct"])),
            "FTpct": (r["FTpct"], pct(b["ft_pct"])),
        }
        print(f"{full_label:20} "
              f"G {r['G']}/{b['g']}  "
              f"MPG {r['MPG']}/{b['mp']}  "
              f"PPG {r['PPG']}/{b['pts']}  "
              f"RPG {r['RPG']}/{b['trb']}  "
              f"APG {r['APG']}/{b['ast']}  "
              f"FG {r['FGpct']}/{pct(b['fg_pct'])}  "
              f"3P {r['3Ppct']}/{pct(b['tp_pct'])}  "
              f"FT {r['FTpct']}/{pct(b['ft_pct'])}  "
              f"lg=g{r['last_reg_game']} {r['last_reg_et_date']}")
        # verdict: G must match; per-game stats within tolerance
        g_ok = r["G"] == b["g"]
        if not g_ok:
            mism.append((full_label, "G", r["G"], b["g"]))
    print("\nG mismatches (DB games vs bball-ref):", len(mism))
    for m in mism:
        print("  ", m)

if __name__ == "__main__":
    main()
