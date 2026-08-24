"""Audit nba.player_rolling_stats season totals vs basketball-reference.

For a deterministic sample of players per season (26-34 = 2016-17 .. 2024-25),
sum the per-game REG stat columns in player_rolling_stats and compare against
the season totals on the player's basketball-reference page.

Season totals semantics: player_rolling_stats.cum_* are CAREER cumulatives (not
season), so a player's season totals = SUM of the per-game columns over that
player's REG games in that season. Verified: Curry 2016-17 -> 79 GP / 1999 PTS /
324 3PM / 183 PF, exactly bball-ref.

Usage: python app/scripts/audit_nba_rolling_seasons.py [--per-season N] [--seasons 26-34]
Fetches via bbref_fetch (cached, paced). Dry audit (read-only) always.
"""
import argparse
import logging
import re
import sys
from sqlalchemy import create_engine, text

sys.path.insert(0, "app/scripts")
from bbref_fetch import fetch_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.audit_nba_rolling")
DUMP = []

SYNC_DATABASE_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

BR_BASE = "https://www.basketball-reference.com/players"

# stat keys pulled from bball-ref data-stat; 'g'/'gs' come via <th> label handling
BB_TOTALS_STATS = ["g", "gs", "mp", "fg", "fga", "fg_pct", "fg3", "fg3a", "fg3_pct",
                   "ft", "fta", "ft_pct", "orb", "drb", "trb", "ast", "stl", "blk",
                   "tov", "pf", "pts"]
# our rolling per-game columns that sum to season totals
PGS_STAT_COLS = ["points", "rebounds_total", "assists", "steals", "blocks",
                 "turnovers", "fouls_personal", "fgm", "fga", "tpm", "tpa",
                 "ftm", "fta", "minutes"]
BB_STAT_TO_COL = {"pts": "pts", "trb": "trb", "ast": "ast",
                  "stl": "stl", "blk": "blk", "tov": "tov",
                  "pf": "pf", "fg": "fgm", "fga": "fga",
                  "fg3": "tpm", "fg3a": "tpa", "ft": "ftm", "fta": "fta", "mp": "mp"}


def parse_bb_totals(html, season_label):
    """Return dict of REG-season totals for season_label (e.g. '2016-17') or None."""
    tabs = re.findall(r'<table[^>]*id="[a-z0-9]+"[^>]*>(.*?)</table>', html, re.S)
    for t in tabs:
        hd = re.search(r"<thead>(.*?)</thead>", t, re.S)
        stats = re.findall(r'<th[^>]*data-stat="([^"]+)"', hd.group(1) if hd else "")
        if "pts" in stats and "trb" in stats and "ast" in stats and "tov" in stats:
            body = re.search(r"<tbody[^>]*>(.*?)</tbody>", t, re.S)
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1) if body else "", re.S)
            for r in rows:
                lbl = re.sub(r"<[^>]+>", "", "".join(re.findall(r"<th[^>]*>(.*?)</th>", r, re.S))).strip()
                if lbl != season_label:
                    continue
                # handle 'g'/'gs' as <th> (data-stat) OR within <td> cells
                vals = {}
                for k, v in re.findall(r'<th[^>]*data-stat="([^"]+)"[^>]*>(.*?)</th>', r, re.S):
                    vals[k] = re.sub(r"<[^>]+>", "", v).strip()
                for k, v in re.findall(r'<td[^>]*data-stat="([^"]+)"[^>]*>(.*?)</td>', r, re.S):
                    vals[k] = re.sub(r"<[^>]+>", "", v).strip()
                return vals
    return None


def player_season_totals(conn, player_id, season_id):
    """SUM per-game REG columns for one player+season."""
    q = text(f"""
        SELECT COUNT(*) AS g,
               COALESCE(SUM(pg.points),0) AS pts,
               COALESCE(SUM(pg.rebounds_total),0) AS trb,
               COALESCE(SUM(pg.assists),0) AS ast,
               COALESCE(SUM(pg.steals),0) AS stl,
               COALESCE(SUM(pg.blocks),0) AS blk,
               COALESCE(SUM(pg.turnovers),0) AS tov,
               COALESCE(SUM(pg.fouls_personal),0) AS pf,
               COALESCE(SUM(pg.fgm),0) AS fgm, COALESCE(SUM(pg.fga),0) AS fga,
               COALESCE(SUM(pg.tpm),0) AS tpm, COALESCE(SUM(pg.tpa),0) AS tpa,
               COALESCE(SUM(pg.ftm),0) AS ftm, COALESCE(SUM(pg.fta),0) AS fta,
               COALESCE(SUM(pg.minutes),0) AS mp
        FROM nba.player_rolling_stats pg
        JOIN nba.games g ON g.id = pg.game_id AND g.game_type = 'REG'
        WHERE pg.player_id=:p AND pg.season_id=:s
    """)
    return conn.execute(q, {"p": player_id, "s": season_id}).first()


def pick_sample(conn, seasons, per_season):
    """Deterministic sample per season: top scorer + per_season-1 mid/role players."""
    picks = []
    for s in seasons:
        # top scorers with br_id
        top = conn.execute(text("""
            SELECT p.id, p.name, p.br_id, ROUND(SUM(pg.points) FILTER (WHERE g.game_type='REG'))
            FROM nba.players p
            JOIN nba.player_rolling_stats pg ON pg.player_id=p.id AND pg.season_id=:s
            JOIN nba.games g ON g.id=pg.game_id
            WHERE p.br_id IS NOT NULL
            GROUP BY p.id,p.name,p.br_id
            HAVING COUNT(DISTINCT CASE WHEN g.game_type='REG' THEN pg.game_id END)>=30
            ORDER BY ROUND(SUM(pg.points) FILTER (WHERE g.game_type='REG')) DESC
            LIMIT :n
        """), {"s": s, "n": per_season}).fetchall()
        for r in top:
            picks.append((s, r[0], r[1], r[2], r[3]))
    return picks


def fmt_pct(v):
    try:
        return f"{float(v)*100:.1f}%"
    except Exception:
        return v


# Games that are real, bet-on REG games in OUR training set but which basketball-reference
# EXCLUDES from its "regular season totals" tab (NBA Cup Finals + 2020 play-in).
# Per Rich's decision (2026-08-23): KEEP them as REG — they're in the training set, so their
# stats belong in the season aggregates. The audit must therefore compare our totals against
# bball-ref's REG-season totals PLUS these special-game lines (not bball-ref's stripped view).
# Map season_id -> {game_id: (team_id, team_id)} both teams in that special game.
SPECIAL_GAMES = {
    29: {50629: (21, 27)},   # 2020 play-in POR(21) v MEM(27)
    33: {24608: (11, 18)},   # 2023 Cup Final LAL(11) v IND(18)
    34: {26067: (13, 24)},   # 2024 Cup Final MIL(13) v OKC(24)
    35: {38347: (16, 23)},   # 2025 Cup Final
}
# bball-ref stat key -> our pgs column for the special-game line we ADD to the baseline
SPECIAL_COLS = {"g": 1, "mp": "minutes", "fg": "fgm", "fga": "fga", "fg3": "tpm",
                "fg3a": "tpa", "ft": "ftm", "fta": "fta", "trb": "rebounds_total",
                "ast": "assists", "stl": "steals", "blk": "blocks", "tov": "turnovers",
                "pf": "fouls_personal", "pts": "points"}


def special_game_line(conn, player_id, season_id):
    """Return dict {bbstat: value} for the player's line in the special (Cup/play-in) game
    of that season, or None if they didn't play in it. Our pgs stores the same per-game line
    the normal REG games do."""
    games = SPECIAL_GAMES.get(season_id)
    if not games:
        return None
    for gid in games:
        row = conn.execute(text(
            """SELECT field_goals_made fgm, field_goals_attempted fga, three_pointers_made tpm,
                       three_pointers_attempted tpa, free_throws_made ftm, free_throws_attempted fta,
                       rebounds_total, assists, steals, blocks, turnovers, fouls_personal,
                       points, minutes
                FROM nba.player_game_stats WHERE game_id=:g AND player_id=:p"""),
            {"g": gid, "p": player_id}).first()
        if row:
            v = {}
            for bstat, ocol in SPECIAL_COLS.items():
                if ocol == 1:
                    v[bstat] = 1  # one game played
                else:
                    v[bstat] = float(getattr(row, ocol) or 0)
            return v
    return None


def _f(bb, key):
    """float of a bball-ref value, treating blank as 0."""
    v = bb.get(key)
    if v is None or v in ("", "."):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-season", type=int, default=5)
    ap.add_argument("--seasons", default="26-34")
    ap.add_argument("--show-ok", action="store_true")
    args = ap.parse_args()
    lo, hi = [int(x) for x in args.seasons.split("-")]

    eng = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
    with eng.connect() as conn:
        sample = pick_sample(conn, range(lo, hi + 1), args.per_season)
    logger.info(f"sample size: {len(sample)} player-seasons (per_season={args.per_season})")

    ok = bad = 0
    for s, pid, name, br_id, our_pts in sample:
        yr = s + 1990  # season_id N => NBA season (N+1990)-(N+1990+1)  [26 -> 2016-17]
        season_label = f"{yr}-{str(yr+1)[-2:]}"
        url = f"{BR_BASE}/{br_id[0]}/{br_id}.html"
        try:
            html = fetch_client.fetch(url)
        except Exception as e:
            logger.warning(f"  fetch fail {name} ({br_id}): {e}"); continue
        bb = parse_bb_totals(html, season_label)
        with eng.connect() as conn:
            ours = player_season_totals(conn, pid, s)
            spl = special_game_line(conn, pid, s)
        if bb is None:
            logger.warning(f"  {name} {season_label}: no bball-ref totals row parsed"); continue

        # If this player appeared in that season's special (Cup final / play-in) game -- which is
        # legitimately in our training set -- add their line to the bball-ref baseline so we
        # compare like-for-like (our full-training-set total vs bball-ref REG + special game).
        if spl is not None:
            for k in list(bb.keys()):
                if k in SPECIAL_COLS and bb.get(k) not in (None, "", "."):
                    try:
                        bb[k] = str(float(bb[k]) + spl[k])
                    except (ValueError, KeyError):
                        pass

        # build diff report
        diffs = []
        # G
        if spl is not None:
            bb_g = _f(bb, "g") + spl.get("g", 0)
        else:
            bb_g = _f(bb, "g")
        g_ours = int(ours.g)
        if bb_g and int(round(bb_g)) != g_ours:
            diffs.append(f"G {g_ours} vs {int(round(bb_g))}")
        for bstat, ocol in BB_STAT_TO_COL.items():
            bv = _f(bb, bstat)
            ov_f = float(getattr(ours, ocol) or 0)
            if abs(bv - ov_f) > 0.51:
                diffs.append(f"{bstat} {ov_f:g} vs {bv:g}")
        # percentages (ours computes from raw sums)
        def pct(num, den):
            return float(num or 0) / float(den or 0)
        ours_pcts = {
            "fg_pct": pct(ours.fgm, ours.fga), "fg3_pct": pct(ours.tpm, ours.tpa), "ft_pct": pct(ours.ftm, ours.fta)}
        for key, ov in ours_pcts.items():
            bv = bb.get(key)
            if bv:
                try:
                    if abs(ov - float(bv)) > 0.005:
                        diffs.append(f"{key} {ov:.3f} vs {float(bv):.3f}")
                except Exception:
                    pass
        if diffs:
            bad += 1
            logger.info(f"  [DIFF] {name} ({br_id}) {season_label}: " + "; ".join(diffs))
        else:
            ok += 1
            if args.show_ok:
                logger.info(f"  [OK] {name} ({br_id}) {season_label}: {ours.g}G {ours.pts}PTS")
    logger.info(f"audit complete: {ok} OK, {bad} with diffs, of {len(sample)}")


if __name__ == "__main__":
    main()
