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
  - DNP_CD   = roster player with didNotPlay=True whose reason is a NON-HEALTH scratch
               (COACH'S DECISION, REST, NOT WITH TEAM, PERSONAL, etc.) — dressed/active.
               Added up to 13 ACTIVE total; unlimited for COVID seasons.
  - INACTIVE = roster player with didNotPlay=True whose reason is a HEALTH/absence reason
               (injury, illness, suspension, health & safety, etc.) — NOT dressed, NOT part
               of the active 13-man roster. NEVER counts toward the 13 cap.

ACTIVE vs INACTIVE split (Rich, 2026-08-29): a dressed(player didNotPlay=True) is ACTIVE
only when the reason is a coach's/load/personal decision. Injury/illness/suspension/health
reasons make the player INACTIVE (does not count toward the 13-man active cap). Classic
boxscore designations: "DNP-COACH'S DECISION" = active; "DNP-LEFT ANKLE SPRAIN" = inactive.

Writes nba.active_players (status PLAYED|DNP_CD|INACTIVE, reason, is_starter, src='postgame').
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


INACTIVE_REASON_KW = (
    "ANKLE", "KNEE", "SHOULDER", "HIP", "FOOT", "HEEL", "TOE", "CALF", "HAMSTRING",
    "WRIST", "THUMB", "FINGER", "FINGERS", "QUAD", "ACHILLES", "BACK", "RIB", "ABDOMEN",
    "GROIN", "ELBOW", "ARM", "LEG", "HAND", "NECK", "CERVICAL", "CONCUSSION", "ILLNESS",
    "INJURY", "SPRAIN", "STRAIN", "SORENESS", "CONTUSION", "FRACTURE", "TENDON",
    "SPASMS", "HEALTH", "SAFETY", "SUSPENDED", "COVID", "DISLOCATION", "SURGERY",
    "LACERATION", "ADDUCTOR", "BURSITIS", "MIGRAINE", "REHAB", "OUT", "PROTOCOL",
    "ARTHRITIS", "ROTATOR", "MCL", "ACL", "LCL", "PCL", "MENISCUS", "PLANTAR",
    "FASCIITIS", "ENTROPION", "RECONDITIONING", "CONDITIONING", "TENDINITIS",
    "BURSITIS", "ILLNAESS",
)


def is_inactive_reason(reason):
    """True when an ESPN didNotPlay reason means the player is NOT active (health/absence).

    Rich (2026-08-29): an active player that did not play carries DNP-COACH'S DECISION.
    Players NOT active carry health/absence notes (DNP-LEFT ANKLE SPRAIN, illness, suspension,
    health & safety, rehab/reconditioning). We invert that: a didNotPlay=True player is
    INACTIVE when the reason indicates health/availability (injury, illness, suspension,
    health & safety, rehab) AND is NOT a coach/load/personal scratch. Coach's decision, REST,
    PERSONAL, NOT WITH TEAM, LOAD MANAGEMENT (non-health) all stay ACTIVE (DNP_CD).
    Empty/None reason can't prove ill -> defaults to ACTIVE (matches historic pre-reason builds).
    """
    if not reason:
        return False  # cannot prove injury -> treat as active scratch (historic default)
    u = reason.upper()
    # Explicit non-health scratch keywords -> ACTIVE (DNP_CD). Keep these precise so they
    # never collide with injury strings (e.g. "LEFT KNEE INJURY MANAGEMENT" must stay INACTIVE,
    # so we match "LOAD MANAGEMENT" not bare "MANAGEMENT").
    if any(k in u for k in ("COACH", "REST", "PERSONAL", "NOT WITH TEAM",
                            "LOAD MANAGEMENT", "DND", "OUT - COMPANY")):
        return False
    return any(k in u for k in INACTIVE_REASON_KW)


def is_played_minutes(m):
    m = (m or "").strip()
    return m not in ("", "-", "0", "0:00", "None")


COVID_SEASONS = {30, 31, 32}  # COVID/hardship seasons: ESPN documents >13 ACTIVE routinely (NBA relaxed the limit). Keep ALL dressed healthy scratches so the roster matches reality.
ACTIVE_CAP = 13  # NBA hard cap for REG/PLAYIN/POST (non-COVID)


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
        # 2) Split didNotPlay=True players by reason (Rich, 2026-08-29):
        #    - non-health scratch (COACH'S DECISION / REST / PERSONAL / NOT WITH TEAM...) ->
        #      DNP_CD  = dressed/ACTIVE healthy scratch, counts toward the 13-man cap.
        #    - health/absence reason (injury/illness/suspension/health...) ->
        #      INACTIVE = NOT dressed, NEVER counts toward the 13-man cap.
        dnp_candidates = []      # active healthy scratches (DNP_CD)
        inactive_rows = []       # not active (INACTIVE)
        for en in roster.get("entries", []):
            espn_pid = en.get("playerId")
            if not espn_pid:
                continue
            pid = pl_map.get(espn_pid)
            if pid is None or pid in played:
                continue
            if en.get("didNotPlay"):
                reason = en.get("reason")
                starter = starter_by_pid.get(pid, False)
                if is_inactive_reason(reason):
                    inactive_rows.append((our_team_id, pid, starter, "INACTIVE", reason))
                else:
                    dnp_candidates.append((pid, starter, reason))
        # 3) ACTIVE roster rule (Rich, 2026-08-22): keep the 13-player hard cap, UNLESS
        #    more than 13 players actually recorded minutes (e.g. season-finale roster
        #    flexibility, two-way callups) — then keep ALL who actually played (authoritative).
        #    INACTIVE players are added OUTSIDE the cap and never consume roster room.
        n_played = len(played_rows)
        active.extend(played_rows)
        active.extend(inactive_rows)
        if n_played > ACTIVE_CAP:
            # more than 13 truly played -> keep every played player, no DNP_CD (all used up)
            pass
        else:
            room = ACTIVE_CAP - n_played        # fill up to 13 with dressed healthy scratches
            if is_covid:
                room = len(dnp_candidates)      # COVID/hardship: ESPN shows >13 active; keep ALL dressed healthy scratches (matches reality)
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
