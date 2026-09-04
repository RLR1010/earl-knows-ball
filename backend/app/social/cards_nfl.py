"""
NFL game_writeups social card — same look as the MLB card but with the ORANGE
Earl portrait and NFL team logos (ESPN CDN) + football stat windows.

Mirrors the MLB `app/social/cards.py::generate_game_card` contract so the
writeups router / nfl generator can call it identically. The MLB game-card
path is untouched; this is a sport-parameterized sibling.

Functions:
    generate_nfl_game_card(game_id, engine=None) -> dict
        Renders a 1600x900 NFL social card to var/cards/nfl/gw-{game_id}.png,
        updates nfl.game_writeups.preview_image, and returns a dict with the
        relative card path + the away/home team cards (parity w/ MLB).
    nfl_team_cards(game_id, conn_or_engine=None) -> dict
        Returns the two team cards + title/dek for rendering in article HTML.
"""
from __future__ import annotations

import base64
import datetime
import pathlib
from urllib.request import Request, urlopen

from sqlalchemy import create_engine, text

from app.social.cards import (  # reuse shared render + template machinery
    CARDS_DIR,
    LOCKUP_PNG,
    TEMPLATE,
    render_png,
    _engine,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
ASSETS = ROOT / "app" / "social" / "assets"
EARL_NFL = ASSETS / "earl-nfl.png"  # orange portrait (companion to MLB earl.png)


def _data_uri(path: pathlib.Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _espn_logo(abbr: str) -> str:
    """Public official NFL logo (browser-loadable), mirroring the MLB mlbstatic URI."""
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"


def _fetch_logo_uri(url: str) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as r:
            b = r.read()
        return "data:" + (r.headers.get_content_type() or "image/png") + ";base64," + base64.b64encode(b).decode()
    except Exception:
        return ""  # missing logo -> empty (fall back to just-name layout)


# Stat label wording swapped for football (template is baseball-worded by default).
_TEMPLATE_VARS = ("Runs Allowed/G Last 5", "Runs Scored/G Last 5", "AVG Last 10")
_NFL_LABELS = ("Points Allowed/G Last 5", "Points Scored/G Last 5", "Win % Last 5")


def _base_html() -> str:
    tpl = TEMPLATE.read_text() if TEMPLATE.exists() else ""
    if not tpl:
        raise RuntimeError(f"missing card template: {TEMPLATE}")
    html = tpl.replace("GAME WRITEUP · MLB · {DATE}", "GAME WRITEUP · NFL · {DATE}")
    for old, new in zip(_TEMPLATE_VARS, _NFL_LABELS):
        html = html.replace(old, new)
    return html


def _resolve_teams(conn, game_id: int) -> dict:
    """Load the NFL game + both teams + leak-safe pre-game record and rolling
    windows (off_pts_r5 / def_pts_r5 / win_pct_r5) exactly like the MLB game
    card path but through the NFL rolling table keyed by team_abbr + game_date.
    """
    g = conn.execute(
        text(
            """SELECT g.id AS game_id, g.date AS as_of,
                      at.abbreviation AS away_abbr, at.name AS away_name,
                      at.conference AS away_conf, at.division AS away_div,
                      ht.abbreviation AS home_abbr, ht.name AS home_name,
                      ht.conference AS home_conf, ht.division AS home_div
               FROM nfl.games g
               JOIN nfl.teams at ON at.id = g.away_team_id
               JOIN nfl.teams ht ON ht.id = g.home_team_id
               WHERE g.id = :gid"""
        ),
        {"gid": int(game_id)},
    ).mappings().first()
    if not g:
        raise RuntimeError(f"nfl game {game_id} not found")

    as_of = g["as_of"]

    def side(side):
        abbr = g[f"{side}_abbr"]
        abbr = (abbr or "").upper()
        name = g[f"{side}_name"]
        conf = (g.get(f"{side}_conf") or "").strip().upper()
        div = (g.get(f"{side}_div") or "").strip()
        div_label = f"{conf} {div}".strip() if div else conf
        # rolling row for the last completed game feeding INTO this one
        # (leak-safe prior-row read via the feeds_into pointer; excludes the
        # target game's own roll-forward row).
        row = conn.execute(
            text(
                """SELECT season_wins, season_losses, off_pts_r5,
                          def_pts_r5, win_pct_r5
                   FROM nfl.team_rolling_stats
                   WHERE team_abbr = :a AND feeds_into_game_id = :gid
                   ORDER BY game_date DESC LIMIT 1"""
            ),
            {"a": abbr, "gid": int(game_id)},
        ).mappings().first()
        meta = div_label
        ra5 = rs5 = avg10 = "–"
        name_disp = name or abbr
        if row:
            w = int(row["season_wins"] or 0)
            l = int(row["season_losses"] or 0)
            meta = f"{w}-{l}" + (f" · {div_label}" if div_label else "")
            rs5 = f"{float(row['off_pts_r5'] or 0):.1f}"
            ra5 = f"{float(row['def_pts_r5'] or 0):.1f}"
            avg10 = f"{float(row['win_pct_r5'] or 0) * 100:.0f}%"
        return {
            "abbr": abbr,
            "name": name_disp,
            "meta": meta,
            "ra5": ra5,
            "rs5": rs5,
            "avg10": avg10,
            "division": div_label,
            "logo_url": _espn_logo(abbr),
        }

    away = side("away")
    home = side("home")

    # headline/dek: prefer the writeup title/seo_description; else matchup fallback.
    title = dek = ""
    w = conn.execute(
        text(
            "SELECT title, seo_description FROM nfl.game_writeups "
            "WHERE game_id = :gid ORDER BY version DESC LIMIT 1"
        ),
        {"gid": int(game_id)},
    ).mappings().first()
    if w:
        title = (w["title"] or "").strip()
        dek = (w["seo_description"] or "").strip()
    if not title:
        title = f"{away['name']} at {home['name']}"
    if not dek:
        dek = "A key AFC matchup with playoff stakes. Full analysis and picks for premium members."
    return {
        "game_id": int(g["game_id"]),
        "as_of": as_of,
        "title": title,
        "dek": dek[:220],
        "away": away,
        "home": home,
    }


def _sub_nfl(html: str, card: dict, date_str: str) -> str:
    away, home = card["away"], card["home"]
    lockup = _data_uri(LOCKUP_PNG, "image/png") if LOCKUP_PNG.exists() else ""
    earl = _data_uri(EARL_NFL, "image/png") if EARL_NFL.exists() else ""
    away_logo = _fetch_logo_uri(away["logo_url"]) if away["logo_url"] else ""
    home_logo = _fetch_logo_uri(home["logo_url"]) if home["logo_url"] else ""
    repl = {
        "{LOGO_SRC}": lockup, "{EARL_SRC}": earl,
        "{DATE}": date_str,
        "{HEADLINE}": card["title"], "{DEK}": card["dek"],
        "{AWAY_LOGO}": away_logo, "{AWAY_NAME}": away["name"],
        "{AWAY_META}": away["meta"], "{AWAY_RA5}": away["ra5"],
        "{AWAY_RS5}": away["rs5"], "{AWAY_AVG10}": away["avg10"],
        "{HOME_LOGO}": home_logo, "{HOME_NAME}": home["name"],
        "{HOME_META}": home["meta"], "{HOME_RA5}": home["ra5"],
        "{HOME_RS5}": home["rs5"], "{HOME_AVG10}": home["avg10"],
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def game_cards_resolved(game_id: int, engine=None):
    """Resolve + return the NFL team-card dict (no render). Shared by both the
    PNG generation and the article-HTML parity path."""
    if engine is None:
        engine = _engine()
    with engine.connect() as conn:
        return _resolve_teams(conn, game_id)


def nfl_team_cards(game_id: int, conn_or_engine=None) -> dict:
    """Return the two team cards + article title/dek (identical to what the
    social-card PNG shows) for rendering in the NFL article HTML.
    Serialized keys match the MLB frontend team_cards contract:
    {game_id, as_of, title, dek, away:{...}, home:{...}}."""
    from sqlalchemy.engine import Connection, Engine
    if conn_or_engine is None:
        conn_or_engine = _engine()
    own = False
    conn = None
    if isinstance(conn_or_engine, Engine):
        conn = conn_or_engine.connect()
        own = True
    elif isinstance(conn_or_engine, Connection):
        conn = conn_or_engine
    try:
        card = _resolve_teams(conn, game_id)
    finally:
        if own:
            conn.close()
    # copy serializable subset (strip any dates/objects the frontend shouldn't see)
    def side(t):
        return {"abbr": t["abbr"], "name": t["name"], "meta": t["meta"],
                "ra5": t["ra5"], "rs5": t["rs5"], "avg10": t["avg10"],
                "division": t["division"], "logo_url": t["logo_url"]}
    return {"game_id": card["game_id"], "title": card["title"], "dek": card["dek"],
            "away": side(card["away"]), "home": side(card["home"])}


def generate_nfl_game_card(game_id: int, engine=None) -> dict:
    """Render the 1600x900 NFL social card PNG and persist preview_image on the
    nfl.game_writeups row (if present). Returns {preview_image, away, home,
    title} parity blob."""
    if engine is None:
        engine = _engine()
    card = game_cards_resolved(game_id, engine=engine)

    date_str = card["as_of"].strftime("%b %d").upper()
    html = _base_html()
    html = _sub_nfl(html, card, date_str)

    out_dir = CARDS_DIR / "nfl"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"gw-{int(game_id)}.png"
    render_png(html, out_path)

    rel = f"/writeups/cards/nfl/gw-{int(game_id)}.png"
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE nfl.game_writeups SET preview_image = :p WHERE game_id = :gid"),
            {"p": rel, "gid": int(game_id)},
        )

    side = lambda t: {"abbr": t["abbr"], "name": t["name"], "meta": t["meta"],
                      "ra5": t["ra5"], "rs5": t["rs5"], "avg10": t["avg10"],
                      "division": t["division"], "logo_url": t["logo_url"]}
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
    return {"preview_image": rel,
            "title": card["title"], "dek": card["dek"],
            "away": side(card["away"]), "home": side(card["home"])}
