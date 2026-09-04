"""Social-card generator for original_articles (editorial).

Renders the single-team "Earl portrait" 16:9 social card (1600x900) that the
frontend admin + public og:image use for an original article.

Design source of truth: /tmp/mock card_portraits.py, and the canonical review it
renders into docs/original-card-templates/portraits/* (per Rich 2026-09-04):
  - Earl wearing a shirt-color portrait bottom-right; team logo on a WHITE circle
    lower-left beside the headline; the theme (gradient + accent) is keyed to THAT
    SHIRT COLOR. Available per sport: mlb/nba -> blue,green,orange,red and
    all/nfl -> blue,green,orange,purple,red.

This module is DEPLOYABLE (self-contained): portraits/logos/lockup load from
backend/app/social/assets (never /tmp at runtime). Shirt-color portraits are
staged under assets/portraits/<sport>-<color>.png. Real team logos not already
in assets are fetched from ESPN's CDN / mlbstatic and cached under
assets/logos/<sport>/<ABBR>.png. Everything is injected as data URIs in one
single fill pass, so there is no token/base64 collision.

Public API used by backend/app/routers/original_articles.py:

    generate_social_card(*, sport, title, dek="", accent="", kicker="FRESH ANGLE",
                         team=None, team_name="", team_meta="",
                         shirt=None, article_id=None, out_png: pathlib.Path) -> str

sport must be one of {"mlb","nba","nfl","all"}; team is an abbreviation and is
optional (no-team -> no logo row). Returns the web-facing relative path
"/writeups/cards/<sport>/original-social-<id>.png" (served LIVE by the backend's
writeups /cards/{sport}/{filename} FileResponse route out of backend/var/cards,
so a freshly generated card is immediately reachable without any Next restart).
"""
from __future__ import annotations

import base64
import pathlib
import re
from typing import Optional
from urllib.request import urlopen


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _ui(color):
    """Return (sub, dim, acc-rgb-tuple) legibility tokens for a shirt theme.
    Values are the owner-approved defaults from /tmp/mock/card_portraits.py
    including its small-tone special-cases for orange/red backgrounds."""
    th = THEME[color]
    if color == "orange":
        sub, dim = "#f2ddc4", "#d9a77b"
    elif color == "red":
        sub, dim = "#f5d7d2", "#b98b86"
    else:
        sub, dim = "#d7dcef", "#97a0bd"
    return sub, dim, _hex_to_rgb(th["acc"])


# Owner-approved per-shirt-color theme (source of truth: /tmp/mock/card_portraits.py
# THEME + its legibility overrides; applied to the golden portrait-card CSS). The
# route picks a color per article; this module only supplies the palette.
THEME = {
    "blue":   dict(b1="#16305e", b0="#0a1830", b2="#040b18", acc="#3f9bff", acc2="#8cc9ff"),
    "green":  dict(b1="#0e3a25", b0="#06251a", b2="#021510", acc="#35cf87", acc2="#8bf0bc"),
    "orange": dict(b1="#4a2410", b0="#2a1507", b2="#15090a", acc="#ff9a3c", acc2="#ffbe8a"),
    "purple": dict(b1="#3a2a63", b0="#241a3f", b2="#120c20", acc="#b39bff", acc2="#d9ccff"),
    "red":    dict(b1="#571f24", b0="#33101a", b2="#170a10", acc="#ff6b6b", acc2="#ffb0ab"),
}

# Colors that have a staged runtime portrait per sport (docs/earl-portraits subset).
# mlb & nba have no purple shirt photo; nfl & "all" have all five.
_SPORT_COLORS = {
    "mlb": ("blue", "green", "orange", "red"),
    "nba": ("blue", "green", "orange", "red"),
    "nfl": ("blue", "green", "orange", "purple", "red"),
    "all": ("blue", "green", "orange", "purple", "red"),
}

# Known subject team (abbr) -> preferred Earl shirt color, per sport. Only a
# curated handful are mapped (most fall back to the stable rotation). Abbrs are
# ambiguous across leagues (KC/LAD/SF...), so keyed by the article's sport.
TEAM_SHIRT = {
    "mlb": {
        "LAD": "blue", "NYY": "blue", "DET": "blue", "CLE": "blue", "CHC": "blue",
        "BOS": "red", "SF": "orange", "LAA": "red", "SEA": "blue", "HOU": "orange",
        "BAL": "orange", "STL": "red", "PHI": "red", "ATL": "blue", "TEX": "red",
        "TOR": "blue", "CIN": "red", "PIT": "red", "NYM": "blue", "WAS": "blue",
        "MIN": "blue", "KC": "blue", "COL": "green", "MIA": "green", "ARI": "red",
        "TB": "blue", "SD": "orange", "MIL": "blue", "CWS": "orange",
        "OAK": "green",
    },
    "nba": {
        "LAL": "blue", "GSW": "blue", "BOS": "green", "MIA": "red", "CHI": "red",
        "CLE": "green", "BKN": "blue", "NYK": "blue", "PHI": "red", "TOR": "red",
        "DAL": "blue", "DEN": "blue", "HOU": "red", "OKC": "blue", "POR": "red",
        "SAS": "blue", "MIL": "green", "MIN": "blue", "PHX": "orange", "LAC": "blue",
        "SAC": "orange", "NOP": "green", "MEM": "orange", "DET": "blue", "ORL": "blue",
        "CHA": "orange", "WAS": "red", "ATL": "green", "IND": "orange", "UTA": "purple",
    },
    "nfl": {
        "KC": "red", "BUF": "blue", "MIA": "orange", "WAS": "red", "PHI": "green",
        "SF": "red", "DAL": "blue", "DET": "blue", "GB": "green", "MIN": "purple",
        "CIN": "orange", "PIT": "orange", "LAR": "blue", "BAL": "purple", "NYJ": "green",
        "NYG": "blue", "CHI": "blue", "CLE": "orange", "ATL": "red", "NO": "green",
        "TB": "red", "CAR": "blue", "HOU": "red", "TEN": "red", "JAX": "orange",
        "IND": "blue", "DEN": "orange", "LV": "purple", "SEA": "blue", "ARI": "red",
        "NE": "blue", "LAC": "purple",
    },
}


def _shirt_colors(sport: str):
    return _SPORT_COLORS.get(sport, _SPORT_COLORS["all"])


def _known_shirt(sport: str, team: str) -> Optional[str]:
    m = TEAM_SHIRT.get(sport, {})
    return m.get((team or "").upper()) or None


def pick_shirt(sport: str, team: Optional[str] = None, article_id: Optional[int] = None) -> str:
    """Deterministically choose the Earl shirt color for an article.

    - A known subject team (teams[0]) maps to its brand-ish shirt color when this
      sport has a runtime photo for that color.
    - Otherwise use a stable rotation seeded by article_id so different articles
      visibly use different shirts for the same sport; a no-team fallback picks the
      sport's first neutral color deterministically.
    """
    colors = _shirt_colors(sport)
    if team:
        want = _known_shirt(sport, team)
        if want and want in colors:
            return want
        if not want and article_id is None:
            # unknown team, no seed -> stable per-team pick
            return colors[sum(map(ord, team.upper())) % len(colors)]
    if article_id is not None:
        return colors[article_id % len(colors)]
    return colors[0]


def _portrait_uri(sport: str, color: str) -> str:
    """Resolve the staged runtime portrait asset for a (sport, shirt-color).
    Falls back to '' (callers then use the neutral assets)."""
    p = ASSETS_DIR / "portraits" / ("%s-%s.png" % (sport, color))
    return _load_uri(p, "image/png") if p.exists() else ""


def _theme_for(sport: str, color: str) -> dict:
    """Merge the chosen shirt palette into a CSS-ready theme dict (with label/full
    from the sport SCHEMES so the chip/team-meta text stays league-accurate)."""
    sc = SCHEMES.get(sport, SCHEMES["all"])
    th = THEME[color]
    sub, dim, acc_rgb = _ui(color)
    chips = "rgba(%d,%d,%d,.38)" % acc_rgb
    return dict(
        b1=th["b1"], b0=th["b0"], b2=th["b2"],
        acc=th["acc"], acc2=th["acc2"],
        chip=chips, sub=sub, dim=dim,
        label=sc["label"], full=sc["full"],
    )

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]           # backend/
ASSETS_DIR = pathlib.Path(__file__).resolve().parent / "assets"
# Original-article social cards are written into the SAME live-served dir the
# game-writeup cards use (backend/var/cards), served on demand by the writeups
# /cards/{sport}/{filename} FileResponse route. Serving from Next's static
# public/ dir instead breaks for newly generated files (Next only indexes
# public/ subdirs at server boot -> 404 on fresh cards until a restart).
_CARDS_DIR = ROOT / "var" / "cards"

SUPPORTED = ("mlb", "nba", "nfl", "all")

# Schemes (identical values to docs/original-card-templates per Rich 2026-09-04).
SCHEMES = {
    "mlb": dict(b1="#0c3523", b0="#061f14", b2="#031209", acc="#2fd37f", acc2="#8bf0bc",
                chip="rgba(47,211,127,.35)", sub="#bde6d2", dim="#6f9c87", label="MLB", full="BASEBALL"),
    "nba": dict(b1="#16305f", b0="#0b1c38", b2="#050d1e", acc="#f27a21", acc2="#ffb87f",
                chip="rgba(242,122,33,.40)", sub="#c6d3ec", dim="#7f92ba", label="NBA", full="BASKETBALL"),
    "nfl": dict(b1="#1a2740", b0="#0c1426", b2="#050810", acc="#d22638", acc2="#ff857b",
                chip="rgba(210,38,56,.42)", sub="#ccd3e4", dim="#868fa8", label="NFL", full="FOOTBALL"),
    "all": dict(b1="#282356", b0="#171340", b2="#0a0920", acc="#8b72ff", acc2="#cdc3ff",
                chip="rgba(139,114,255,.40)", sub="#cbc7ef", dim="#827fa9", label="ALL", full="CROSS-SPORT"),
}


def _load_uri(path: pathlib.Path, mime: str) -> str:
    return ("data:%s;base64," % mime) + base64.b64encode(path.read_bytes()).decode()


def _rgb(hexstr: str) -> str:
    h = hexstr.lstrip("#")
    return "%d,%d,%d" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _local_logo(abbr: str, sport: str) -> str:
    key = (abbr or "").lower()
    for p in sorted(ASSETS_DIR.glob("**/" + key + "." + ("svg" if False else "*"))):
        if p.suffix.lower() in (".svg", ".png") and p.name.lower().startswith(key):
            mime = "image/svg+xml" if p.suffix.lower() == ".svg" else "image/png"
            return _load_uri(p, mime)
    return ""


def _fetch_logo_uri(abbr: str, sport: str) -> str:
    urls = ["https://a.espncdn.com/i/teamlogos/%s/500/scoreboard/%s.png" % (sport, abbr)]
    if sport == "mlb":
        urls.insert(0, "https://a.espncdn.com/i/teamlogos/mlb/500/scoreboard/%s.png" % abbr)
    for url in urls:
        try:
            data = urlopen(url, timeout=8).read()
            if not data:
                continue
            d = ASSETS_DIR / "logos" / sport
            d.mkdir(parents=True, exist_ok=True)
            p = d / ("%s.png" % abbr)
            p.write_bytes(data)
            return _load_uri(p, "image/png")
        except Exception:
            continue
    return ""


def logo_uri(abbr: Optional[str], sport: str) -> str:
    if not abbr:
        return ""
    return _local_logo(abbr, sport) or _fetch_logo_uri(abbr, sport) or ""


def accented_title(title: str, accent: str) -> str:
    """Wrap the (first, case-insensitive) occurrence of accent in <em>. Plain otherwise."""
    if not accent or not title:
        return title
    m = re.search(re.escape(accent), title, re.IGNORECASE)
    if not m:
        return title
    return (
        title[: m.start()]
        + "<em>"
        + title[m.start(): m.end()]
        + "</em>"
        + title[m.end():]
    )


def _ss_css(c, b0rgb, glowrgb) -> str:
    return (
        "*{margin:0;padding:0;box-sizing:border-box;"
        "font-family:'Arial','Segoe UI',Roboto,Helvetica,sans-serif}"
        "html,body{width:1600px;height:900px}"
        ".card{position:relative;width:1600px;height:900px;overflow:hidden;color:#eef3fb;"
        "background:linear-gradient(150deg,BC1 0%,BC0 55%,BC2 100%)}"
        ".glow{position:absolute;left:-160px;bottom:-250px;width:1020px;height:1020px;"
        "border-radius:50%;background:radial-gradient(circle, rgba(GLOW,0.30) 0%, transparent 62%)}"
        ".earl{position:absolute;right:0;bottom:0;height:600px;z-index:2}"
        ".earl img{height:100%;width:auto;display:block}"
        ".veil{position:absolute;right:0;bottom:0;height:100%;width:380px;z-index:3;"
        "pointer-events:none;background:linear-gradient(270deg, rgba(B0R,0.82) 0%, rgba(B0R,0) 62%)}"
        ".inner{position:relative;z-index:5;height:100%;padding:58px 104px;display:flex;flex-direction:column}"
        ".topbar{display:flex;align-items:center;justify-content:space-between}"
        ".lockup{height:52px}"
        ".chip{color:SUB;font-size:18px;font-weight:700;letter-spacing:2px;"
        "border:1px solid CHIP;padding:8px 18px;border-radius:40px}"
        ".mid{flex:1;display:flex;flex-direction:column;justify-content:center;max-width:1120px;padding-top:6px}"
        ".kicker{color:ACC2;font-size:20px;font-weight:800;letter-spacing:3px;"
        "text-transform:uppercase;margin-bottom:24px}"
        "h1{color:#fff;font-size:74px;font-weight:900;line-height:1.03;letter-spacing:-1px;max-width:1120px}"
        "h1 em{color:ACC2;font-style:normal}"
        ".dek{margin-top:30px;color:SUB;font-size:28px;line-height:1.42;max-width:1120px}"
        ".teamrow{margin-top:40px;display:flex;align-items:center;gap:20px}"
        ".circle{width:104px;height:104px;border-radius:50%;background:#fff;display:flex;"
        "align-items:center;justify-content:center;box-shadow:0 8px 26px rgba(0,0,0,.45)}"
        ".circle img{width:66px;height:66px;object-fit:contain}"
        ".tn{font-size:23px;font-weight:900;color:#fff;letter-spacing:1px}"
        ".ts{font-size:15px;font-weight:700;color:DIM;letter-spacing:3px;margin-top:3px}"
        ".foot{display:flex;align-items:center;gap:18px;padding-bottom:4px}"
        ".foot .a{font-size:20px;font-weight:800;letter-spacing:3px}"
        ".foot .b{font-size:17px;font-weight:600;letter-spacing:2px;color:DIM}"
    ).replace("BC1", c["b1"]).replace("BC0", c["b0"]).replace("BC2", c["b2"]) \
     .replace("B0R", b0rgb).replace("GLOW", glowrgb) \
     .replace("SUB", c["sub"]).replace("CHIP", c["chip"]).replace("DIM", c["dim"]) \
     .replace("ACC2", c["acc2"])


def _build_html(*, c: dict, label: str, full_label: str, earl_uri: str, lock_uri: str,
                kicker: str, title: str, dek_html: str, team_row: str) -> str:
    css = _ss_css(c, _rgb(c["b0"]), _rgb(c["acc"]))
    team_row_safe = ("<div class=\"teamrow\">%s</div>" % team_row) if team_row else ""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>%s</style></head><body>"
        "<div class=\"card\">"
        "<div class=\"glow\"></div>"
        "<div class=\"earl\"><img src=\"%s\"/></div>"
        "<div class=\"veil\"></div>"
        "<div class=\"inner\">"
        "<div class=\"topbar\"><img class=\"lockup\" src=\"%s\"/><span class=\"chip\">%s&nbsp;&bull;&nbsp;%s</span></div>"
        "<div class=\"mid\">"
        "<span class=\"kicker\">%s</span>"
        "<h1>%s</h1>"
        "%s"
        "%s"
        "</div>"
        "<div class=\"foot\"><span class=\"a\">EARL KNOWS BALL</span><span class=\"b\">ORIGINAL ANALYSIS</span></div>"
        "</div></div></body></html>"
    ) % (css, earl_uri, lock_uri, label, full_label, kicker, title, dek_html, team_row_safe)


def _screenshot(html: str, out_png: pathlib.Path) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.set_content(html, wait_until="networkidle", timeout=40000)
        page.screenshot(path=str(out_png))
        browser.close()


def generate_social_card(
    *,
    sport: str,
    title: str,
    dek: str = "",
    accent: str = "",
    kicker: str = "FRESH ANGLE",
    team: Optional[str] = None,
    team_name: str = "",
    team_meta: str = "",
    shirt: Optional[str] = None,     # desired Earl shirt color (else auto-picked)
    article_id: Optional[int] = None,  # stable seed for color fallback / file name
    out_png: pathlib.Path,
) -> str:
    """Render the portrait editorial card to out_png and return its /og/... relative path.

    ``out_png``'s parent directory must already exist (the caller creates it under
    frontend/public/og/<sport>/). sport falls back to "all" when unknown. When
    ``shirt`` is omitted the shirt color is chosen deterministically from the subject
    team (teams[0]) or a stable rotation keyed by article_id.
    """
    sport = sport if sport in SCHEMES else "all"
    color = shirt if (shirt and shirt in THEME and shirt in _shirt_colors(sport)) \
        else pick_shirt(sport, team, article_id)
    c = _theme_for(sport, color)

    # --- portrait & lockup (deployable assets) -----------
    staged = _portrait_uri(sport, color)
    earl_uri = staged  # data URI of the staged shirt-color portrait
    if not earl_uri:
        # safety net: neutral legacy assets are kept intact & never deleted
        for cand in (
            ASSETS_DIR / "earl-original-card.png",
            ASSETS_DIR / "earl.png",
            ASSETS_DIR / "earl-nba.png",
        ):
            if cand.exists():
                earl_uri = _load_uri(cand, "image/png")
                break
    lock_path = ASSETS_DIR / "logo_lockup.png"
    lock_uri = _load_uri(lock_path, "image/png") if lock_path.exists() else ""

    title_html = accented_title(title, accent)

    # --- team row (single team) -------------------------
    lg = ""
    team_row = ""
    if team:
        lg = logo_uri(team, sport if sport != "all" else "all")
        name = (team_name or team).upper()
        meta = (team_meta or c["full"]).upper()
        if lg:
            team_row = ('<div class="circle"><img src="%s"/></div>'
                        '<div><div class="tn">%s</div><div class="ts">%s</div></div>'
                        % (lg, name, meta))
        else:
            team_row = ('<div class="circle"></div>'
                        '<div><div class="tn">%s</div><div class="ts">%s</div></div>'
                        % (name, meta))

    full_label = c["full"]
    dek_html = ("<p class=\"dek\">%s</p>" % dek) if (dek or "").strip() else ""
    html = _build_html(
        c=c,
        label=c["label"], full_label=full_label,
        earl_uri=earl_uri, lock_uri=lock_uri,
        kicker=kicker, title=title_html,
        dek_html=dek_html,
        team_row=team_row,
    )
    _screenshot(html, out_png)

    # /writeups/cards/<sport>/... (served live by writeups FileResponse route)
    return "/writeups/cards/%s/%s" % (sport, out_png.name)


# Convenience for callers that want to write under og/<sport>/
def compute_out_path(*, article_id, sport="all") -> pathlib.Path:
    d = _CARDS_DIR / sport
    d.mkdir(parents=True, exist_ok=True)
    return d / ("original-social-%d.png" % article_id)
