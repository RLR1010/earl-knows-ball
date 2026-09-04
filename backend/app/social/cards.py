#!/usr/bin/env python3
"""Generate the 16:9 social card (og:image) for an original_articles matchup.

Reads the article (by id or slug for a sport), derives the two team abbreviations
from its `teams` JSON, resolves each team's real division + win% and the fixed
stat set from the sport's rolling-stats table, composes the social-card HTML,
screenshots it to a 1600x900 PNG with Playwright under frontend/public/og, and
writes the relative URL into original_articles.preview_image.

Fixed stat set rendered on EVERY card (deterministic, not LLM-chosen):
  - Runs Allowed/G, last 5   (rolling col ra5)
  - Runs Scored/G, last 5    (rolling col rf5)
  - AVG, last 10             (rolling col avg10)

Maps into the MLB rolling table today; NBA/NFL can extend SPORT spec.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python -m app.social.cards mlb 113
  cd backend && PYTHONPATH=$PWD ../venv/bin/python -m app.social.cards mlb <slug>
"""
import argparse
import asyncio
import base64
import json
import pathlib
import sys
from datetime import datetime, timezone

from sqlalchemy import text, create_engine

from app.core.config import settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
ASSETS = ROOT / "app" / "social" / "assets"
FRONT_PUBLIC = ROOT.parent / "frontend" / "public" / "og"   # legacy original_articles cards
CARDS_DIR = ROOT / "var" / "cards"                            # compute-owned; served by writeups router
TEMPLATE = ROOT / "app" / "social" / "card_template.html"

LOCKUP_PNG = ASSETS / "logo_lockup.png"
EARL_PNG = ASSETS / "earl.png"

MLB_LOGO = {  # official mlbstatic team id by abbr (fallback; prefers local svg)
    "LAD": 119, "DET": 116, "HOU": 117, "NYY": 147, "ATL": 144,
    "CLE": 114, "CHC": 112, "BOS": 111, "SF": 137, "PHI": 143,
    "ARI": 109, "BAL": 110, "CWS": 145, "CIN": 113, "COL": 115,
    "MIA": 146, "MIL": 158, "MIN": 142, "NYM": 121, "OAK": 133,
    "SD": 135, "SEA": 136, "STL": 138, "TB": 139, "TEX": 140,
    "TOR": 141, "WAS": 120, "KC": 118, "PIT": 134, "LAA": 108,
}


def _data_uri(path: pathlib.Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _logo_uri(abbr: str) -> str:
    local = ASSETS / f"{abbr.upper()}.svg"
    if local.exists():
        return _data_uri(local, "image/svg+xml")
    import urllib.request
    tid = MLB_LOGO.get(abbr.upper())
    if not tid:
        return ""
    svg = urllib.request.urlopen(
        f"https://www.mlbstatic.com/team-logos/{tid}.svg", timeout=15).read()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode()


def _engine():
    return create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))


def load_article(conn, sport: str, ref: str) -> dict:
    row = conn.execute(
        text(
            "SELECT id, sport, title, summary, slug, teams "
            "FROM public.original_articles "
            "WHERE sport=:sport AND (slug=:slug OR id=:id) LIMIT 1"
        ),
        {"sport": sport, "slug": ref, "id": int(ref) if ref.isdigit() else 0},
    ).mappings().first()
    if not row:
        raise SystemExit(f"article not found: {sport}/{ref}")
    a = dict(row)
    t = a.get("teams")
    a["teams"] = json.loads(t) if isinstance(t, str) else (list(t) if t else [])
    return a


def team_stats(conn, sport: str, abbr: str, as_of=None) -> dict:
    """Return {name, meta, ra5, rs5, avg10} for one MLB team.

    `as_of` may be a datetime/date — stat windows are taken from the rolling row
    closest BEFORE that date (leak-safe: rows are inclusive of their own game, so
    we read the most recent row strictly before as_of to avoid peeking ahead).
    When as_of is None the latest available rolling row is used.
    """
    abbr = abbr.upper()
    if sport != "mlb":
        raise SystemExit(f"cards.py: sport '{sport}' not wired yet (add to SPORT spec)")
    t = conn.execute(
        text("SELECT id, name FROM mlb.teams WHERE abbreviation=:a"),
        {"a": abbr},
    ).mappings().first()
    out = {"abbr": abbr, "name": abbr, "meta": "", "ra5": "–", "rs5": "–", "avg10": "–"}
    if not t:
        return out
    s = _stat_windows(conn, t["id"], as_of)
    if s:
        out["name"] = t["name"]
        out["ra5"] = f"{float(s['ra5'] or 0):.1f}"
        out["rs5"] = f"{float(s['rf5'] or 0):.1f}"
        out["avg10"] = f"{float(s['avg10'] or 0):.3f}".lstrip("0") or "0"
    return out


def _stat_windows(conn, team_id: int, as_of=None):
    """Fetch the ra5/rf5/avg10 stat windows for a team, leak-safe to `as_of`."""
    if as_of is not None:
        return conn.execute(
            text("SELECT ra5, rf5, avg10 FROM mlb.team_rolling_stats "
                 "WHERE team_id=:tid AND game_date < :asof AND ra5 IS NOT NULL "
                 "ORDER BY game_date DESC LIMIT 1"),
            {"tid": team_id, "asof": as_of},
        ).mappings().first()
    return conn.execute(
        text("SELECT ra5, rf5, avg10 FROM mlb.team_rolling_stats "
             "WHERE team_id=:tid AND ra5 IS NOT NULL ORDER BY game_date DESC LIMIT 1"),
        {"tid": team_id},
    ).mappings().first()


def _sub(html: str, article: dict, away: dict, home: dict, date_str: str) -> str:
    lockup = _data_uri(LOCKUP_PNG, "image/png") if LOCKUP_PNG.exists() else ""
    earl = _data_uri(EARL_PNG, "image/png") if EARL_PNG.exists() else ""
    repl = {
        "{LOGO_SRC}": lockup, "{EARL_SRC}": earl,
        "{DATE}": date_str,
        "{HEADLINE}": article.get("title", ""), "{DEK}": article.get("_dek", article.get("summary", "")),
        "{AWAY_LOGO}": _logo_uri(away["abbr"]), "{AWAY_NAME}": away["name"],
        "{AWAY_META}": away["meta"], "{AWAY_RA5}": away["ra5"],
        "{AWAY_RS5}": away["rs5"], "{AWAY_AVG10}": away["avg10"],
        "{HOME_LOGO}": _logo_uri(home["abbr"]), "{HOME_NAME}": home["name"],
        "{HOME_META}": home["meta"], "{HOME_RA5}": home["ra5"],
        "{HOME_RS5}": home["rs5"], "{HOME_AVG10}": home["avg10"],
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def render_png(html: str, out_path: pathlib.Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        pg.set_content(html, wait_until="load")
        pg.wait_for_timeout(250)
        pg.screenshot(path=str(out_path))
        b.close()


def run(sport: str, ref: str) -> str:
    engine = _engine()
    with engine.connect() as conn:
        article = load_article(conn, sport, ref)
        if len(article["teams"]) < 2:
            raise SystemExit(f"{ref} has {len(article['teams'])} teams; need 2 for a card")
        away = team_stats(conn, sport, article["teams"][0])
        home = team_stats(conn, sport, article["teams"][1])

    def dek_of(a):
        d = (a.get("summary") or "").strip()
        t = (a.get("title") or "").strip()
        if t and d.startswith(t):
            d = d[len(t):].lstrip(" .:…—-·")
        return d
    article["_dek"] = dek_of(article)

    date_str = datetime.now(timezone.utc).strftime("%b %d").upper()
    tpl = TEMPLATE.read_text() if TEMPLATE.exists() else ""
    if not tpl:
        raise SystemExit(f"missing template: {TEMPLATE}")
    html = _sub(tpl, article, away, home, date_str)

    sport_dir = sport if sport in ("mlb",) else "og"
    out_dir = FRONT_PUBLIC / "previews" / sport_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{article['id']}.png"
    render_png(html, out_path)
    rel = f"/og/previews/{sport_dir}/{article['id']}.png"

    with engine.begin() as conn:
        conn.execute(text("UPDATE public.original_articles SET preview_image=:p WHERE id=:i"),
                     {"p": rel, "i": article["id"]})
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
    print(f"preview_image = {rel}")
    return rel


# ── MLB game_writeups social card ───────────────────────────────────
# Re-targets the same template/assets/Playwright machinery from run() (which is
# legacy, keyed to original_articles) at mlb.game_writeups, keyed by game_id.

def load_game_writeup(conn, game_id: int) -> dict:
    """Resolve away/home abbrs + the writeup headline/dek for one MLB game.

    Returns {game_id, away_abbr, home_abbr, title, dek}. Raises SystemExit if the
    game (with both teams) is missing. A missing writeup row is tolerated (the
    card can be rendered with an empty headline/dek fallback)."""
    g = conn.execute(
        text(
            "SELECT g.id AS game_id, g.date AS as_of, "
            "       at.abbreviation AS away_abbr, at.name AS away_name, "
            "       at.league AS away_league, at.division AS away_division, "
            "       g.away_wins, g.away_losses, "
            "       ht.abbreviation AS home_abbr, ht.name AS home_name, "
            "       ht.league AS home_league, ht.division AS home_division, "
            "       g.home_wins, g.home_losses "
            "FROM mlb.games g "
            "JOIN mlb.teams ht ON ht.id = g.home_team_id "
            "JOIN mlb.teams at ON at.id = g.away_team_id "
            "WHERE g.id = :gid LIMIT 1"
        ),
        {"gid": int(game_id)},
    ).mappings().first()
    if not g:
        raise SystemExit(f"mlb game not found (or missing teams): {game_id}")
    w = conn.execute(
        text(
            "SELECT title, seo_description, public_content "
            "FROM mlb.game_writeups WHERE game_id = :gid "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"gid": int(game_id)},
    ).mappings().first()
    title = (w["title"] if w else "") or ""
    # DEK: prefer seo_description; else a short lead pulled from public_content.
    dek = ""
    if w:
        dek = (w["seo_description"] or "").strip()
        if not dek:
            body = (w["public_content"] or "").strip()
            # strip a leading markdown heading/line if it echoes the title
            for line in body.splitlines():
                line = line.strip().lstrip("#").strip()
                if line and line.lower() != title.strip().lower():
                    dek = line
                    break
        if title and dek.startswith(title):
            dek = dek[len(title):].lstrip(" .:…—-·")
    return {
        "game_id": int(g["game_id"]),
        "as_of": g["as_of"],
        # pre-game record stored on the game row (authoritative for historical)
        "away": {
            "abbr": (g["away_abbr"] or "").upper(),
            "name": g["away_name"],
            "league": (g["away_league"] or "").upper(),
            "division": (g["away_division"] or "").strip(),
            "wins": int(g["away_wins"] or 0),
            "losses": int(g["away_losses"] or 0),
        },
        "home": {
            "abbr": (g["home_abbr"] or "").upper(),
            "name": g["home_name"],
            "league": (g["home_league"] or "").upper(),
            "division": (g["home_division"] or "").strip(),
            "wins": int(g["home_wins"] or 0),
            "losses": int(g["home_losses"] or 0),
        },
        "title": title,
        "dek": dek[:220],
    }


def _build_team_card(conn, team) -> dict:
    """Compose one side's public team-card dict from a ``load_game_writeup``
    team row. Shared by ``generate_game_card`` (PNG) and ``game_team_cards``
    (article HTML) so the two can never drift.

    meta = the ROW's pre-game W-L record (from mlb.games away/home_wins+losses)
    + division. Stat windows (ra5/rf5/avg10) from the rolling table, leak-safe
    to the game's own date (as_of).
    """
    d = {"abbr": team["abbr"], "name": team["name"] or team["abbr"],
         "meta": "", "ra5": "–", "rs5": "–", "avg10": "–", "logo_url": None}
    _tid = MLB_LOGO.get(team["abbr"].upper())
    if _tid:
        # Public official MLB logo URL (browser-loadable; matches the card's src).
        d["logo_url"] = f"https://www.mlbstatic.com/team-logos/{_tid}.svg"
    lg = (team.get("league") or "").upper()
    dv = (team.get("division") or "").strip()
    div_label = (f"{lg} {dv}".strip() if dv else lg)
    w = team.get("wins")
    l = team.get("losses")
    if w is not None and l is not None:
        d["meta"] = f"{int(w)}-{int(l)}" + (f" · {div_label}" if div_label else "")
    else:
        d["meta"] = div_label
    from sqlalchemy import text as _txt
    tid_row = conn.execute(
        _txt("SELECT id FROM mlb.teams WHERE abbreviation=:a"), {"a": team["abbr"]}
    ).mappings().first()
    s = _stat_windows(conn, tid_row["id"], team.get("as_of")) if tid_row else None
    if s:
        d["ra5"] = f"{float(s['ra5'] or 0):.1f}"
        d["rs5"] = f"{float(s['rf5'] or 0):.1f}"
        d["avg10"] = f"{float(s['avg10'] or 0):.3f}".lstrip("0") or "0"
    return d


def game_team_cards(game_id: int, conn_or_engine=None) -> dict:
    """Return the two team cards + article title/dek for an MLB game_writeup,
    identical to what the social-card PNG shows, for rendering in the article
    HTML. Only MLB is wired. Returns serializable dict:
    {game_id, as_of, title, dek, away:{...}, home:{...}}.
    """
    if conn_or_engine is None:
        conn_or_engine = _engine()
    from sqlalchemy.engine import Connection, Engine
    own = False
    if isinstance(conn_or_engine, Engine):
        cm = conn_or_engine.connect(); conn = cm.__enter__()
        _close = lambda: cm.__exit__(None, None, None); own = True
    elif isinstance(conn_or_engine, Connection):
        conn = conn_or_engine; _close = lambda: None
    else:
        e = _engine(); cm = e.connect(); conn = cm.__enter__()
        _close = lambda: cm.__exit__(None, None, None); own = True
    try:
        info = load_game_writeup(conn, game_id)
        away_t = dict(info["away"]); away_t["as_of"] = info.get("as_of")
        home_t = dict(info["home"]); home_t["as_of"] = info.get("as_of")
        return {
            "game_id": info["game_id"],
            "as_of": info.get("as_of"),
            "title": info["title"],
            "dek": info["dek"],
            "away": _build_team_card(conn, away_t),
            "home": _build_team_card(conn, home_t),
        }
    finally:
        _close()
        if own:
            conn.engine.dispose()


def generate_game_card(sport: str, game_id: int, conn_or_engine=None) -> str:
    """Render the social/og card for an MLB game_writeup, keyed by game_id.

    - Resolves away/home team abbrs from mlb.games + mlb.teams.
    - Reuses team_stats() (ra5/rf5/avg10 + win% + division/league) for each team.
    - Reuses the shared template/assets + Playwright render.
    - Writes PNG to frontend/public/og/previews/mlb/gw-{game_id}.png.
    - Sets mlb.game_writeups.preview_image and returns the site-relative URL.

    `conn_or_engine` may be a SQLAlchemy Engine or Connection; if None, a sync
    engine is created. Only MLB is wired (team_stats() raises for other sports)."""
    if sport != "mlb":
        raise SystemExit(f"generate_game_card: sport '{sport}' not wired (mlb only)")

    own_engine = None
    if conn_or_engine is None:
        own_engine = _engine()
        conn_or_engine = own_engine

    # Accept either an Engine (open a connection) or a live Connection.
    from sqlalchemy.engine import Connection, Engine
    if isinstance(conn_or_engine, Engine):
        _cm = conn_or_engine.connect()
        conn = _cm.__enter__()
        _close = lambda: _cm.__exit__(None, None, None)
        engine = conn_or_engine
    elif isinstance(conn_or_engine, Connection):
        conn = conn_or_engine
        _close = lambda: None
        engine = conn.engine
    else:  # unknown; fall back to a fresh engine
        own_engine = _engine()
        engine = own_engine
        _cm = engine.connect()
        conn = _cm.__enter__()
        _close = lambda: _cm.__exit__(None, None, None)

    def _team_dict(team):
        """Compose {abbr,name,meta,ra5,rs5,avg10} for one side.

        meta = the ROW's pre-game W-L record (from mlb.games away/home_wins+losses,
        which is captured when a card is generated and is correct even for a
        historical game) + division. Stat windows come from the rolling table,
        leak-safe to the game's own date (as_of) so nothing after that game leaks.
        """
        d = {"abbr": team["abbr"], "name": team["name"] or team["abbr"],
             "meta": "", "ra5": "–", "rs5": "–", "avg10": "–"}
        lg = (team.get("league") or "").upper()
        dv = (team.get("division") or "").strip()
        div_label = (f"{lg} {dv}".strip() if dv else lg)
        w = team.get("wins")
        l = team.get("losses")
        if w is not None and l is not None:
            d["meta"] = f"{int(w)}-{int(l)}" + (f" · {div_label}" if div_label else "")
        else:
            d["meta"] = div_label
        # stat windows by abbr, leak-safe to the game date
        tid_row = conn.execute(
            text("SELECT id FROM mlb.teams WHERE abbreviation=:a"), {"a": team["abbr"]}
        ).mappings().first()
        s = _stat_windows(conn, tid_row["id"], info.get("as_of")) if tid_row else None
        if s:
            d["ra5"] = f"{float(s['ra5'] or 0):.1f}"
            d["rs5"] = f"{float(s['rf5'] or 0):.1f}"
            d["avg10"] = f"{float(s['avg10'] or 0):.3f}".lstrip("0") or "0"
        return d

    try:
        info = load_game_writeup(conn, game_id)
        away = _team_dict(info["away"])
        home = _team_dict(info["home"])
    finally:
        _close()

    article = {"title": info["title"], "summary": info["dek"], "_dek": info["dek"]}
    date_str = datetime.now(timezone.utc).strftime("%b %d").upper()
    tpl = TEMPLATE.read_text() if TEMPLATE.exists() else ""
    if not tpl:
        raise SystemExit(f"missing template: {TEMPLATE}")
    html = _sub(tpl, article, away, home, date_str)

    out_dir = CARDS_DIR / "mlb"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"gw-{info['game_id']}.png"
    render_png(html, out_path)
    rel = f"/writeups/cards/mlb/gw-{info['game_id']}.png"

    with engine.begin() as wconn:
        wconn.execute(
            text("UPDATE mlb.game_writeups SET preview_image=:p WHERE game_id=:g"),
            {"p": rel, "g": info["game_id"]},
        )
    if own_engine is not None:
        own_engine.dispose()
    return rel


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sport")
    ap.add_argument("ref", help="original_articles id/slug, or (with --game) an mlb game_id")
    ap.add_argument("--game", action="store_true",
                    help="treat ref as an mlb.games game_id and render the game_writeups card")
    a = ap.parse_args()
    if a.game:
        rel = generate_game_card(a.sport, int(a.ref))
        print(f"preview_image = {rel}")
    else:
        run(a.sport, a.ref)
