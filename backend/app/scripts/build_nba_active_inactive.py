"""Build the accurate NBA per-game active/inactive classification from ESPN core API.

Source: sports.core.api.espn.com (the v3 core API, NOT the 403-blocked site.api.espn.com).
Game roster:   .../competitions/{esid}/competitors/{espn_team_id}/roster
     entry fields: playerId, displayName, active, starter, didNotPlay, reason

ACCURACY RULE (Rich, 2026-08-22): keep the 13-player hard cap UNLESS more than 13 players
actually recorded minutes (final-game roster flexibility, hardship, two-way callups) — then
keep ALL who really played (authoritative). Preseason is excluded (no active limit + not in
training data). COVID seasons (30-32) allow extra dressed scratches via hardship exceptions.

Classification (builds ONLY REG + PLAYIN + POST games):
  - PLAYED   = pgs minutes > 0 (ground truth, game-scoped). Always included. If >13 played,
               ALL are kept (no artificial cap) and no DNP_CD is added.
  - DNP_CD   = roster player with didNotPlay=True (dressed/active, healthy scratch) who did
               NOT play, added up to 13 ACTIVE total; unlimited for COVID seasons.
  - INACTIVE = intentionally empty (see note). Real pregame inactive/injury list needs
               the live pregame availability feed, not this historical backfill.

Writes nba.active_players (status PLAYED|DNP_CD, reason, is_starter, src='postgame').
Backup of the old table: nba.active_players_bak_20260822_1803 (270,021 rows).
Idempotent per game (DELETE + re-insert). Resumable.
"""
import sys, os, argparse, json, time, httpx, logging
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "backend"))
sys.path.insert(0, os.path.join(REPO, "backend", "app"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
log = logging.getLogger("earl.nba_active_inactive")

from sqlalchemy import create_engine, text
from app.db_urls import SYNC_DATABASE_URL

CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "application/json"}


def client():
    return httpx.Client(headers=UA, timeout=30, follow_redirects=True)


def get_json(s, url, retries=3):
    for i in range(retries):
        try:
            r = s.get(url)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 403, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def team_abbr_map(engine):
    """Map ESPN/nba abbreviations -> our canonical nba.teams.id.
    ESPN core API uses different abbreviations than our table (GS vs GSW, NY vs NYK,
    SA vs SAS, NO vs NOP, WSH vs WAS, UTAH vs UTA). Map them to canonical ids.
    """
    with engine.connect() as c:
        rows = {r[0]: r[1] for r in c.execute(text("SELECT abbreviation, id FROM nba.teams"))}
    # canonical id for each ESPN abbreviation (our table has alias dupes; pick the primary 1-30)
    our_abbr = {r[1]: r[0] for r in rows.items()}  # id -> abbr (last wins; aliases share id? no)
    alias = {
        "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP",
        "WSH": "WAS", "UTAH": "UTA", "PHX": "PHX", "NOLA": "NOP", "BKN": "BKN",
    }
    out = {}
    for abbr, tid in rows.items():
        canon = alias.get(abbr, abbr)
        out[abbr] = tid
    # add aliases pointing to the canonical team of their canonical abbr
    for espn_ab, canonical_ab in alias.items():
        if canonical_ab in rows:
            out[espn_ab] = rows[canonical_ab]
    # ensure every alias resolves (our primary abbrs already in out)
    return out


def player_espn_map(engine):
    with engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(
            text("SELECT espn_id, id FROM nba.players WHERE espn_id IS NOT NULL"))}


def played_by_team_map(engine, gid):
    """{our_team_id: set(our_player_id)} from pgs minutes > 0 (ground truth PLAYED)."""
    out = {}
    with engine.connect() as c:
        for tid, pid, minutes in c.execute(text(
            "SELECT team_id, player_id, minutes FROM nba.player_game_stats WHERE game_id=:g"), {"g": gid}):
            m = (minutes or "").strip()
            if m in ("", "-", "0", "0:00", "None"):
                continue
            out.setdefault(tid, set()).add(pid)
    return out


def is_played_minutes(m):
    m = (m or "").strip()
    return m not in ("", "-", "0", "0:00", "None")


COVID_SEASONS = {30, 31, 32}
ACTIVE_CAP = 13  # NBA hard cap for non-COVID REG/PLAYIN/POST


def process_game(s, esid, tm_map, pl_map, abbr_cache, season_id, played_by_team):
    """Classify one game's active roster. Returns list of active_rows:
    (our_team_id, player_id, is_starter, status, reason), or None on API failure.
    """
    comp = get_json(s, f"{CORE}/events/{esid}/competitions/{esid}")
    if not comp:
        return None
    comps = comp.get("competitors")
    if not comps:
        return None
    is_covid = season_id in COVID_SEASONS
    active = []
    for co in comps:
        coo = get_json(s, co["$ref"])
        if not coo:
            continue
        espn_team_id = coo.get("id")
        ab = abbr_cache.get(espn_team_id)
        if ab is None:
            tmo = get_json(s, coo.get("team", {}).get("$ref"))
            ab = (tmo or {}).get("abbreviation")
            if ab:
                abbr_cache[espn_team_id] = ab
        if not ab:
            continue
        our_team_id = tm_map.get(ab.upper())
        if our_team_id is None:
            log.warning("  no nba.teams match for abbrev %r (esid %s)", ab, esid)
            continue
        roster = get_json(s, f"{CORE}/events/{esid}/competitions/{esid}/competitors/{espn_team_id}/roster")
        if not roster:
            continue
        played = played_by_team.get(our_team_id, set())
        # Build player_id -> is_starter map from the roster's entry-level `starter`
        # flag (authoritative: exactly 5 starters per team per game). This applies to
        # BOTH played players and dressed scratches.
        starter_by_pid = {}
        for en in roster.get("entries", []):
            espn_pid = en.get("playerId")
            if not espn_pid:
                continue
            pid = pl_map.get(espn_pid)
            if pid is None:
                continue
            starter_by_pid[pid] = bool(en.get("starter"))
        # 1) every PLAYED player (pgs ground truth) -> status PLAYED
        played_rows = [
            (our_team_id, pid, starter_by_pid.get(pid, False), "PLAYED", None)
            for pid in played
        ]
        # 2) DNP_CD candidates from roster: didNotPlay=True, no pgs minutes, healthy scratch
        dnp_candidates = []
        for en in roster.get("entries", []):
            espn_pid = en.get("playerId")
            if not espn_pid:
                continue
            pid = pl_map.get(espn_pid)
            if pid is None or pid in played:
                continue
            if en.get("didNotPlay"):
                dnp_candidates.append((pid, starter_by_pid.get(pid, False), en.get("reason")))
        # 3) ACTIVE roster rule (Rich, 2026-08-22): keep the 13-player hard cap, UNLESS
        #    more than 13 players actually recorded minutes (e.g. season-finale roster
        #    flexibility, two-way callups) — then keep ALL who actually played (authoritative).
        n_played = len(played_rows)
        if n_played > ACTIVE_CAP:
            # more than 13 truly played -> keep every played player, no DNP_CD (all used up)
            active.extend(played_rows)
        else:
            active.extend(played_rows)
            room = ACTIVE_CAP - n_played        # fill up to 13 with dressed healthy scratches
            if is_covid:
                room = len(dnp_candidates)      # COVID hardship: allow all dressed scratches too
            for pid, starter, reason in dnp_candidates[:room]:
                active.append((our_team_id, pid, starter, "DNP_CD", reason))
    return active


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="26-35")
    ap.add_argument("--only-game", default=None, help="one espn game id to process then exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(SYNC_DATABASE_URL)
    tm_map = team_abbr_map(engine)
    pl_map = player_espn_map(engine)
    log.info("teams=%d players(espn_id)=%d", len(tm_map), len(pl_map))

    lo, hi = args.seasons.split("-")
    lo, hi = int(lo), int(hi)

    with engine.connect() as c:
        if args.only_game:
            rows = c.execute(text(
                "SELECT id, nba_game_id, season_id, game_type FROM nba.games "
                "WHERE nba_game_id=:e AND status='FINAL'"), {"e": args.only_game}).fetchall()
        else:
            rows = c.execute(text(
                "SELECT id, nba_game_id, season_id, game_type FROM nba.games "
                "WHERE status='FINAL' AND nba_game_id IS NOT NULL "
                "AND game_type IN ('REG','PLAYIN','POST') "
                "AND season_id BETWEEN :lo AND :hi ORDER BY season_id, date"),
                {"lo": lo, "hi": hi}).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    log.info("processing %d games (seasons %s)", len(rows), args.seasons)

    s = client()
    abbr_cache = {}  # espn_team_id -> abbreviation (cached)
    n_total = n_dnp = n_fail = 0
    for gid, esid, season_id, game_type in rows:
        played_by_team = played_by_team_map(engine, gid)
        act = process_game(s, esid, tm_map, pl_map, abbr_cache, season_id, played_by_team)
        if act is None:
            n_fail += 1
            log.warning("  FAIL esid=%s (gid=%s)", esid, gid)
            continue
        if args.dry_run:
            dnp = sum(1 for a in act if a[3] == "DNP_CD")
            log.info("  dry esid=%s active=%d (dnp_cd=%d)", esid, len(act), dnp)
            n_total += len(act); n_dnp += dnp
            continue
        with engine.begin() as c:
            c.execute(text("DELETE FROM nba.active_players WHERE game_id=:g"), {"g": gid})
            c.execute(text("DELETE FROM nba.inactive_players WHERE game_id=:g"), {"g": gid})
            for team_id, pid, starter, status, reason in act:
                c.execute(text(
                    "INSERT INTO nba.active_players (game_id, team_id, player_id, is_starter, status, reason, src) "
                    "VALUES (:g, :t, :p, :s, :st, :r, 'postgame') "
                    "ON CONFLICT (game_id, player_id) DO UPDATE SET "
                    "team_id=EXCLUDED.team_id, is_starter=EXCLUDED.is_starter, status=EXCLUDED.status, "
                    "reason=EXCLUDED.reason, src=EXCLUDED.src, updated_at=now()"),
                    {"g": gid, "t": team_id, "p": pid, "s": starter, "st": status, "r": reason})
        n_total += len(act); n_dnp += sum(1 for a in act if a[3] == "DNP_CD")
        time.sleep(0.1)
        if (n_total + n_fail) % 250 == 0 and (n_total + n_fail) > 0:
            log.info("  ...processed %d active rows / %d fails so far", n_total, n_fail)

    log.info("DONE active=%d dnp_cd=%d fails=%d", n_total, n_dnp, n_fail)
    s.close()


if __name__ == "__main__":
    main()
