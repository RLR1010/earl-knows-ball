"""Backfill 2016/2017 NBA games with total_turnovers + estimated_possessions
from basketball-reference game logs.

ESPN provides NO per-team box stats (fouls/turnovers/possessions) for games
before ~2018 (404 / empty), so these two seasons' `total_turnovers` and
`estimated_possessions` columns are sparse. basketball-reference game logs have:

  - basic gamelog:      per-game team `tov` (sums to season team total; == total turnovers)
  - advanced gamelog:   per-game `pace` (already normalized to 48 min)

Possessions per game = pace * (48 + 5*n_OT) / 48. For a regulation game that is
just `pace`. We write these into nba.games as total_turnovers / estimated_possessions.

Only games with NULL total_turnovers OR NULL estimated_possessions are updated.
"""
import asyncio
import datetime as _dt
import re

import httpx

from sqlalchemy import create_engine, text

from app.db_urls import PSYCOPG2_DATABASE_URL

# season_id -> (bball season-end year number) used in URL /teams/{ABBR}/{YEAR}/
#   season 26 = real 2016-17 -> URL year 2017
#   season 27 = real 2017-18 -> URL year 2018
SEASON_YEAR = {26: 2017, 27: 2018}

# our nba.teams.abbreviation -> basketball-reference gamelog opp_name_abbr
ABBR_ALIAS = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO"}

# disk cache dir for resumability (parsed team-season game logs)
CACHE_DIR = "/tmp/nba_bball_gamelog_cache"


def bball_abbr(ab):
    return ABBR_ALIAS.get(ab, ab)


async def fetch_team_season_persist(client, sid, abbr, year):
    """Fetch+parse a team-season, persist to disk, return (basic, adv)."""
    import os, json
    os.makedirs(CACHE_DIR, exist_ok=True)
    cachefile = os.path.join(CACHE_DIR, f"{sid}_{abbr}.json")
    # convert tuple keys to "ymd|opp" strings for JSON, and back on load
    def _enc(d):
        return {"|".join(k): v for k, v in d.items()}

    def _dec(d):
        return {tuple(k.split("|")): v for k, v in d.items()}

    if os.path.exists(cachefile):
        with open(cachefile) as fh:
            d = json.load(fh)
        return _dec(d["basic"]), _dec(d["adv"])
    bbb = bball_abbr(abbr)
    basic_html = await fetch(client, f"{BASE}/teams/{bbb}/{year}/gamelog/")
    await asyncio.sleep(5.0)
    adv_html = await fetch(client, f"{BASE}/teams/{bbb}/{year}/gamelog-advanced/")
    basic = parse_gamelog(basic_html, "team_game_log_reg")
    adv = parse_advanced(adv_html, "team_game_log_adv_reg")
    with open(cachefile, "w") as fh:
        json.dump({"basic": _enc(basic), "adv": _enc(adv)}, fh)
    return basic, adv

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
BASE = "https://www.basketball-reference.com"


def _txt(row, name):
    # cell may be plain text or an <a> link; strip tags, HTML entities, whitespace
    m = re.search(r'data-stat="' + name + r'"[^>]*>(.*?)<\/td>', row, re.S)
    if not m:
        return None
    val = re.sub(r"<[^>]+>", "", m.group(1))
    val = re.sub(r"&nbsp;|&#160;|\u00a0", "", val)
    val = val.strip()
    return val or None



def parse_gamelog(html, table_pref):
    """Return dict: (date_mmdd_opp) -> {tov, opp_tov} from basic log."""
    # date comes as /boxscores/201610250GSW.html -> 20161025
    rows = re.findall(rf'<tr id="{table_pref}\.?\d*".*?</tr>', html, re.S)
    out = {}
    for r in rows:
        date = _txt(r, "date")   # e.g. "201610250GSW" or "/boxscores/201610250GSW.html"
        opp = _txt(r, "opp_name_abbr")
        if not date or not opp:
            continue
        ymd = re.search(r"(20\d{2})-(\d{2})-(\d{2})", date)
        if not ymd:
            # fallback: maybe a bare YYYYMMDD appeared
            ymd2 = re.search(r"(20\d{6})", date)
            if ymd2:
                key = (ymd2.group(1), opp)
            else:
                continue
        else:
            key = (ymd.group(1) + ymd.group(2) + ymd.group(3), opp)
        tov = _txt(r, "tov")
        otov = _txt(r, "opp_tov")
        prev = out.get(key, {})
        if tov:
            prev["tov"] = tov
        if otov:
            prev["opp_tov"] = otov
        out[key] = prev
    return out


def parse_advanced(html, table_pref):
    """Return dict: (ymd, opp) -> {pace, minutes} minutes derived from overtimes."""
    rows = re.findall(rf'<tr id="{table_pref}\.?\d*".*?</tr>', html, re.S)
    out = {}
    for r in rows:
        date = _txt(r, "date")
        opp = _txt(r, "opp_name_abbr")
        if not date or not opp:
            continue
        ymd = re.search(r"(20\d{2})-(\d{2})-(\d{2})", date)
        if not ymd:
            ymd2 = re.search(r"(20\d{6})", date)
            if ymd2:
                ymdkey = ymd2.group(1)
            else:
                continue
        else:
            ymdkey = ymd.group(1) + ymd.group(2) + ymd.group(3)
        ot = _txt(r, "overtimes") or ""
        mins = 48
        # overtimes like "OT", "2OT", "3OT" or the total? default reg = 48
        if "2" in ot and "ot" in ot.lower():
            mins = 48 + 10
        elif ot.lower().startswith("ot"):
            mins = 48 + 5
        pace = _txt(r, "pace")
        try:
            pacef = float(pace) if pace else None
        except ValueError:
            pacef = None
        out[(ymdkey, opp)] = {"pace": pacef, "minutes": mins}
    return out


async def fetch(client, url, timeout=300):
    """Fetch a URL; on 429 wait a long time and retry forever (never gives up).
    This handles bball-ref's persistent IP rate-limit blocks by waiting them out.
    """
    waited = 0
    while True:
        r = await client.get(url)
        if r.status_code == 200:
            return r.text
        if r.status_code == 429:
            # IP is rate-limited for a while; wait 4 min between retries
            await asyncio.sleep(240)
            waited += 240
            continue
        await asyncio.sleep(6)
        if waited > timeout:
            raise TimeoutError(f"timed out ({timeout}s) waiting out rate-limit for {url}")


async def fetch_team_season(client, abbr, year):
    bbb = bball_abbr(abbr)  # bball-ref abbr for the team's own gamelog URL
    basic_html = await fetch(client, f"{BASE}/teams/{bbb}/{year}/gamelog/")
    adv_html = await fetch(client, f"{BASE}/teams/{bbb}/{year}/gamelog-advanced/")
    basic = parse_gamelog(basic_html, "team_game_log_reg")
    adv = parse_advanced(adv_html, "team_game_log_adv_reg")
    return basic, adv


async def main():
    engine = create_engine(PSYCOPG2_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    # Gather missing games per season
    missing = {26: {}, 27: {}}  # gid -> (home_team_id, away_team_id, date_ymd)
    with engine.connect() as c:
        for sid in SEASON_YEAR:
            rows = c.execute(text(
                """SELECT g.id, ht.abbreviation, at.abbreviation, to_char(g.date,'YYYYMMDD')
                   FROM nba.games g
                   JOIN nba.teams ht ON ht.id=g.home_team_id
                   JOIN nba.teams at ON at.id=g.away_team_id
                   WHERE g.season_id=:s AND g.game_type='REG'
                     AND (g.home_total_turnovers IS NULL OR g.home_estimated_possessions IS NULL)
                """), {"s": sid}).fetchall()
            for gid, hab, aab, dymd in rows:
                missing[sid][gid] = (hab, aab, dymd)

    total_todo = sum(len(v) for v in missing.values())
    print(f"games to fill: season26={len(missing[26])} season27={len(missing[27])} total={total_todo}")

    filled = 0
    async with httpx.AsyncClient(timeout=40, headers=UA, follow_redirects=True) as client:
        # team data cache: (sid,abbr) -> (basic, adv)
        cache = {}
        # keep a set of game ids not yet fillable (missing both team logs)
        for sid in SEASON_YEAR:
            year = SEASON_YEAR[sid]
            # fetch every team involved first (dedup by (sid,abbr)); commits incrementally
            for gid, (hab, aab, dymd) in missing[sid].items():
                for abbr in (hab, aab):
                    if (sid, abbr) not in cache:
                        print(f"[s{sid}] fetching {abbr}/{year} (cache={len(cache)})...", flush=True)
                        basic, adv = await fetch_team_season_persist(client, sid, abbr, year)
                        cache[(sid, abbr)] = (basic, adv)
                        await asyncio.sleep(5.0)

            # build game -> values and commit per-team as data becomes available
            newfor = []
            for gid, (hab, aab, dymd) in missing[sid].items():
                if (sid, hab) not in cache or (sid, aab) not in cache:
                    continue
                hb, _ = cache[(sid, hab)]
                ab_, _2 = cache[(sid, aab)]
                # our nba.games.date is UTC-shifted +1 day vs the real (bball) date
                # for ~73% of games, so try exact key then the (game - 1 day) fallback.
                opp_bb_a = bball_abbr(aab)
                opp_bb_h = bball_abbr(hab)
                cand = [dymd]
                try:
                    dt = _dt.date(int(dymd[:4]), int(dymd[4:6]), int(dymd[6:8]))
                    cand.append((dt - _dt.timedelta(days=1)).strftime("%Y%m%d"))
                except Exception:
                    pass
                hrec = arec = hv = av = {}
                for ck in cand:
                    if (ck, opp_bb_a) in hb:
                        hrec = hb[(ck, opp_bb_a)]
                        hv = cache[(sid, hab)][1].get((ck, opp_bb_a), {})
                        break
                for ck in cand:
                    if (ck, opp_bb_h) in ab_:
                        arec = ab_[(ck, opp_bb_h)]
                        av = cache[(sid, aab)][1].get((ck, opp_bb_h), {})
                        break
                hpace, hmins = hv.get("pace"), hv.get("minutes", 48)
                apace, amins = av.get("pace"), av.get("minutes", 48)
                home_tov = hrec.get("tov") or arec.get("opp_tov")
                away_tov = hrec.get("opp_tov") or arec.get("tov")
                hposs = round((hpace * hmins / 48), 1) if hpace else None
                aposs = round((apace * amins / 48), 1) if apace else None
                if hposs is None and apace:
                    hposs = round((apace * amins / 48), 1)
                if aposs is None and hpace:
                    aposs = round((hpace * hmins / 48), 1)
                if home_tov and away_tov and hposs and aposs:
                    newfor.append((int(home_tov), int(away_tov), hposs, aposs, gid))

            # commit this season's fills now so partial progress persists
            with engine.begin() as c:
                for home_tov, away_tov, hposs, aposs, gid in newfor:
                    c.execute(text(
                        """UPDATE nba.games
                           SET home_total_turnovers=:h, away_total_turnovers=:a,
                               home_estimated_possessions=:hp, away_estimated_possessions=:ap
                           WHERE id=:id
                             AND (home_total_turnovers IS NULL OR home_estimated_possessions IS NULL)
                        """), {"h": home_tov, "a": away_tov, "hp": hposs, "ap": aposs, "id": gid})
            filled += len(newfor)
            print(f"[s{sid}] committed {len(newfor)} game fills (total filled={filled})", flush=True)
    print(f"DONE: filled {filled}/{total_todo} games total", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
