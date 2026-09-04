"""
NBA game_writeups social card — same look as the MLB card but with the dark-
ORANGE Earl portrait (shared with NFL) and NBA team logos (ESPN CDN) + NBA
stat windows (Off/Def/Net Rating, last 5) + season record.

Mirrors `cards_nfl.py` exactly so the writeups router / nba generator can call
it identically. MLB + NFL card paths are untouched; this is the NBA sibling.

Functions:
    generate_nba_game_card(game_id, engine=None) -> dict
        Renders a 1600x900 NBA social card to var/cards/nba/gw-{game_id}.png,
        updates nba.game_writeups.preview_image, returns {preview_image, away,
        home, title}.
    nba_team_cards(game_id, conn_or_engine=None) -> dict
        Returns the two team cards + title/dek for the article HTML.
"""
from __future__ import annotations

import base64
import pathlib
from urllib.request import Request, urlopen

from sqlalchemy import text

from app.social.cards import (  # reuse shared render + template machinery
    CARDS_DIR,
    LOCKUP_PNG,
    TEMPLATE,
    render_png,
    _engine,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
ASSETS = ROOT / "app" / "social" / "assets"
EARL_NBA = ASSETS / "earl-nba.png"  # dark-orange portrait (NBA shares NFL's art)


def _data_uri(path: pathlib.Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _espn_logo(abbr: str) -> str:
    """Public official NBA logo (browser-loadable) on the ESPN CDN (PNG)."""
    return f"https://a.espncdn.com/i/teamlogos/nba/500/{abbr}.png"


def _fetch_logo_uri(url: str) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as r:
            b = r.read()
        return "data:" + (r.headers.get_content_type() or "image/png") + ";base64," + base64.b64encode(b).decode()
    except Exception:
        return ""  # missing -> empty (just-name layout)


# Baseball-worded template labels replaced with NBA stat wording.
_TEMPLATE_VARS = ("Runs Allowed/G Last 5", "Runs Scored/G Last 5", "AVG Last 10")
_NBA_LABELS = ("Def RTG / G Last 5", "Off RTG / G Last 5", "Net RTG Last 5")


def _base_html() -> str:
    tpl = TEMPLATE.read_text() if TEMPLATE.exists() else ""
    if not tpl:
        raise RuntimeError(f"missing card template: {TEMPLATE}")
    html = tpl.replace("GAME WRITEUP · MLB · {DATE}", "GAME WRITEUP · NBA · {DATE}")
    for old, new in zip(_TEMPLATE_VARS, _NBA_LABELS):
        html = html.replace(old, new)
    return html


def _record_thru(conn, team_id: int, before_date, season_id) -> str:
    """Season W-L for *team_id* up to (not including) *before_date* in *season_id*.
    Counts completed games where the team is home or away and its score is set."""
    row = conn.execute(
        text(
            """SELECT
                 (SELECT count(*) FROM nba.games
                   WHERE season_id = :s AND (:t IN (home_team_id, away_team_id))
                     AND date::date < :bd AND home_score IS NOT NULL
                     AND ((home_team_id = :t AND home_score > away_score)
                       OR (away_team_id = :t AND away_score > home_score))) AS w,
                 (SELECT count(*) FROM nba.games
                   WHERE season_id = :s AND (:t IN (home_team_id, away_team_id))
                     AND date::date < :bd AND home_score IS NOT NULL
                     AND ((home_team_id = :t AND home_score < away_score)
                       OR (away_team_id = :t AND away_score < home_score))) AS l"""
        ),
        {"t": int(team_id), "s": int(season_id), "bd": before_date},
    ).mappings().first()
    return f"{int(row['w'] or 0)}-{int(row['l'] or 0)}"


def _side_meta(conn, team_id: int, abbr: str, name: str, conf: str, div: str,
               before_date, season_id) -> dict:
    """""" 
    row = conn.execute(
        text(
            """SELECT (SELECT ortg_r5 FROM nba.team_rolling_stats
                      WHERE team_id = :tid AND season_id = :s AND game_date < :bd
                      ORDER BY game_date DESC LIMIT 1) AS off_r5,
                      (SELECT drtg_r5 FROM nba.team_rolling_stats
                      WHERE team_id = :tid AND season_id = :s AND game_date < :bd
                      ORDER BY game_date DESC LIMIT 1) AS def_r5,
                      (SELECT net_rtg_r5 FROM nba.team_rolling_stats
                      WHERE team_id = :tid AND season_id = :s AND game_date < :bd
                      ORDER BY game_date DESC LIMIT 1) AS net_r5"""
        ),
        {"tid": int(team_id), "s": int(season_id), "bd": before_date},
    ).mappings().first()
    rec = _record_thru(conn, team_id, before_date, season_id)
    divlabel = (conf or "").strip()
    if div:
        divlabel = f"{div}".strip() or divlabel
    meta = rec + (f" · {divlabel}" if divlabel else "")
    f = lambda v: f"{float(v):.1f}" if v is not None else "–"
    return {
        "abbr": abbr, "name": name, "meta": meta,
        "ra5": f(row["def_r5"]),   # template legacy: ra=allowed -> Def RTG
        "rs5": f(row["off_r5"]),   # rs=scored -> Off RTG
        "avg10": f(row["net_r5"]), # AVG10 -> Net RTG
        "division": div, "logo_url": _espn_logo(abbr), "record": rec,
    }


def _resolve(conn, game_id: int) -> dict:
    g = conn.execute(
        text(
            """SELECT g.id AS game_id, g.date AS as_of, g.date::date AS as_of_day,
                      g.season_id,
                      at.id AS away_team_id, at.abbreviation AS away_abbr,
                      at.name AS away_name, at.conference AS away_conf,
                      at.division AS away_div,
                      ht.id AS home_team_id, ht.abbreviation AS home_abbr,
                      ht.name AS home_name, ht.conference AS home_conf,
                      ht.division AS home_div
               FROM nba.games g
               JOIN nba.teams at ON at.id = g.away_team_id
               JOIN nba.teams ht ON ht.id = g.home_team_id
               WHERE g.id = :gid"""
        ),
        {"gid": int(game_id)},
    ).mappings().first()
    if not g:
        raise RuntimeError(f"nba game {game_id} not found")

    away = _side_meta(conn, g["away_team_id"], g["away_abbr"], g["away_name"],
                      g["away_conf"], g["away_div"], g["as_of"], g["season_id"])
    home = _side_meta(conn, g["home_team_id"], g["home_abbr"], g["home_name"],
                      g["home_conf"], g["home_div"], g["as_of"], g["season_id"])

    title = dek = ""
    w = conn.execute(
        text("SELECT title, seo_description FROM nba.game_writeups "
             "WHERE game_id = :gid ORDER BY version DESC LIMIT 1"),
        {"gid": int(game_id)},
    ).mappings().first()
    if w:
        title = (w["title"] or "").strip()
        dek = (w["seo_description"] or "").strip()
    if not title:
        title = f"{away['name']} at {home['name']}"
    if not dek:
        dek = "A key NBA showdown with playoff stakes. Full analysis and picks for premium members."
    return {
        "game_id": int(g["game_id"]), "as_of": g["as_of"],
        "title": title, "dek": dek[:220], "away": away, "home": home,
    }


def _sub_nba(html: str, card: dict, date_str: str) -> str:
    away, home = card["away"], card["home"]
    lockup = _data_uri(LOCKUP_PNG, "image/png") if LOCKUP_PNG.exists() else ""
    earl = _data_uri(EARL_NBA, "image/png") if EARL_NBA.exists() else ""
    awl = _fetch_logo_uri(away["logo_url"]) if away["logo_url"] else ""
    hol = _fetch_logo_uri(home["logo_url"]) if home["logo_url"] else ""
    repl = {
        "{LOGO_SRC}": lockup, "{EARL_SRC}": earl, "{DATE}": date_str,
        "{HEADLINE}": card["title"], "{DEK}": card["dek"],
        "{AWAY_LOGO}": awl, "{AWAY_NAME}": away["name"], "{AWAY_META}": away["meta"],
        "{AWAY_RA5}": away["ra5"], "{AWAY_RS5}": away["rs5"], "{AWAY_AVG10}": away["avg10"],
        "{HOME_LOGO}": hol, "{HOME_NAME}": home["name"], "{HOME_META}": home["meta"],
        "{HOME_RA5}": home["ra5"], "{HOME_RS5}": home["rs5"], "{HOME_AVG10}": home["avg10"],
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def nba_team_cards(game_id: int, conn_or_engine=None) -> dict:
    from sqlalchemy.engine import Connection, Engine
    if conn_or_engine is None:
        conn_or_engine = _engine()
    own = isinstance(conn_or_engine, Engine)
    conn = conn_or_engine.connect() if own else conn_or_engine
    try:
        card = _resolve(conn, game_id)
    finally:
        if own:
            conn.close()
    side = lambda t: {"abbr": t["abbr"], "name": t["name"], "meta": t["meta"],
                      "ra5": t["ra5"], "rs5": t["rs5"], "avg10": t["avg10"],
                      "division": t["division"], "logo_url": t["logo_url"]}
    return {"game_id": card["game_id"], "title": card["title"], "dek": card["dek"],
            "away": side(card["away"]), "home": side(card["home"])}


def generate_nba_game_card(game_id: int, engine=None) -> dict:
    if engine is None:
        engine = _engine()
    with engine.connect() as conn:
        card = _resolve(conn, game_id)
        date_str = card["as_of"].strftime("%b %d").upper()
        html = _base_html()
        html = _sub_nba(html, card, date_str)

        out_dir = CARDS_DIR / "nba"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"gw-{int(game_id)}.png"
        render_png(html, out_path)

        rel = f"/writeups/cards/nba/gw-{int(game_id)}.png"
        with engine.begin() as uc:
            uc.execute(
                text("UPDATE nba.game_writeups SET preview_image = :p WHERE game_id = :gid"),
                {"p": rel, "gid": int(game_id)},
            )

    side = lambda t: {"abbr": t["abbr"], "name": t["name"], "meta": t["meta"],
                      "ra5": t["ra5"], "rs5": t["rs5"], "avg10": t["avg10"],
                      "division": t["division"], "logo_url": t["logo_url"]}
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
    return {"preview_image": rel,
            "title": card["title"], "dek": card["dek"],
            "away": side(card["away"]), "home": side(card["home"])}
