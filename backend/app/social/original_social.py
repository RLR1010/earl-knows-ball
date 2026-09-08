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


# ============================================================================
# TEMPLATE B — "SCORECARD" for Earl's-Winners recap articles
# ----------------------------------------------------------------------------
# A second card template in the same visual language as the portrait card above,
# but layout-driven by RESULTS rather than a theme: a big W-tally badge plus a
# legible panel of the actual winning picks. It reads instantly as "here is what
# cashed" and is designed to survive EVERY future winners recap from only
# structured winner rows (public.earl_winners), never bespoke per-day artwork.
#
# Winners are ordered newest/best-first and rendered top-to-bottom in a table;
# on a recap with 8 winners we show all 8 (rows are compact). Optional fields
# (score / odds / ev) degrade gracefully: absent ones simply skip their chip, so
# a row never crashes on a sparse snapshot. The Earl portrait is kept as a
# side motif (this is still an Earl card) but the board is the hero.
# ============================================================================

def _sc_css(c: dict, b0rgb: str, accrgb: str) -> str:
    """Scorecard-block styles. Tokens are unique so they never collide with the
    portrait card's (BC0/BC1/ACC...)."""
    glowrgb = "rgba(%s,0.30)" % accrgb
    return (
        "*{margin:0;padding:0;box-sizing:border-box;"
        "font-family:'Arial','Segoe UI',Roboto,Helvetica,sans-serif}"
        "html,body{width:1600px;height:900px}"
        ".card{position:relative;width:1600px;height:900px;overflow:hidden;"
        "color:SCSUB;background:linear-gradient(150deg,SCB1 0%,SCB0 58%,SCB2 100%)}"
        ".glow{position:absolute;left:-150px;bottom:-300px;width:1050px;height:1050px;"
        "border-radius:50%;background:radial-gradient(circle,GLW 0%,transparent 62%)}"
        ".earl{position:absolute;right:-30px;bottom:40px;height:430px;opacity:.9;z-index:1}"
        ".earl img{height:100%;width:auto;display:block;"
        "filter:drop-shadow(0 8px 30px rgba(0,0,0,.4))}"
        ".inner{position:relative;z-index:5;height:100%;padding:44px 64px;"
        "display:flex;flex-direction:column}"
        ".topbar{display:flex;align-items:center;justify-content:space-between}"
        ".lockup{height:50px}"
        ".chip{color:SCSUB;font-size:16px;font-weight:800;letter-spacing:2px;"
        "border:1px solid SCCHIP;padding:7px 16px;border-radius:40px}"
        ".head{display:flex;align-items:flex-start;align-items:center;gap:36px;margin-top:6px}"
        ".copy{display:flex;flex-direction:column}"
        ".kicker{color:SCACC2;font-size:19px;font-weight:800;letter-spacing:3px;"
        "text-transform:uppercase;margin-bottom:10px}"
        "h1{color:#fff;font-size:46px;font-weight:900;line-height:1.06;"
        "letter-spacing:-.5px;max-width:1000px}"
        "h1 em{color:SCACC2;font-style:normal}"
        ".badge{flex:0 0 auto;width:168px;height:168px;border-radius:22px;"
        "background:rgba(255,255,255,.06);border:1px solid SCACC2;"
        "display:flex;flex-direction:column;align-items:center;justify-content:center}"
        ".bnum{font-size:64px;font-weight:900;line-height:.9;color:SCACC}"
        ".blab{font-size:12px;font-weight:800;letter-spacing:2px;color:SCSUB;"
        "text-transform:uppercase;margin-top:6px;text-align:center}"
        ".dek{margin-top:14px;color:SCSUB;font-size:19px;line-height:1.4;max-width:1120px}"
        ".board{margin-top:18px;flex:1;display:flex;flex-direction:column;gap:7px;"
        "padding-right:300px}"   # leave room for the Earl portrait on the right
        ".trow{display:flex;align-items:center;gap:14px;padding:9px 16px;border-radius:12px;"
        "background:rgba(255,255,255,.045)}"
        ".tidx{color:DIM;width:26px;font-weight:800;font-size:15px;text-align:center}"
        ".tlogo{width:38px;height:38px;border-radius:50%;background:#fff;"
        "display:flex;align-items:center;justify-content:center;flex:0 0 auto}"
        ".tlogo img{width:26px;height:26px;object-fit:contain}"
        ".tmeta{display:flex;flex-direction:column;flex:1 1 auto;min-width:0}"
        ".tname{color:#fff;font-weight:800;font-size:17px;letter-spacing:.3px}"
        ".tpick{font-size:12px;font-weight:700;letter-spacing:.5px}"
        ".tscore{color:#fff;font-weight:900;font-size:17px;flex:0 0 auto;min-width:56px}"
        ".todds{color:SCACC2;font-weight:800;font-size:14px;flex:0 0 auto;min-width:56px;"
        "text-align:right}"
        ".tev{font-size:13px;font-weight:800;flex:0 0 auto;min-width:54px;text-align:right}"
        ".tev.pos{color:#ffe14d}"
        ".foot{display:flex;align-items:center;gap:18px;padding-top:14px}"
        ".foot .a{font-size:18px;font-weight:800;letter-spacing:2px}"
        ".foot .b{font-size:15px;font-weight:600;letter-spacing:2px;color:SCDIM}"
    ).replace("SCB1", c["b1"]).replace("SCB0", c["b0"]).replace("SCB2", c["b2"]) \
     .replace("SCACC2", c["acc2"]).replace("SCACC", c["acc"]) \
     .replace("SCCHIP", c["chip"]).replace("SCSUB", c["sub"]).replace("SCDIM", c["dim"]) \
     .replace("GLW", glowrgb)


def _sc_badge_html(wins_total: int, ca: str, cs: str) -> str:
    return (
        "<div class=\"badge\"><div class=\"bnum\">%s</div>"
        "<div class=\"blab\">Earl\'s<br/>Winners</div></div>"
    ) % wins_total


def _sc_row_html(i: int, w: dict) -> str:
    def _s(v):
        return "" if v is None else str(v).strip()
    abbr = (w.get("abbr") or "").upper()
    sport = w.get("sport") or "all"
    logo = logo_uri(abbr, sport)
    pick = _s(w.get("pick"))
    name = _s(w.get("name")) or abbr or ("Pick %d" % i)
    score = _s(w.get("score"))
    odds = _s(w.get("odds"))
    ev = _s(w.get("ev"))
    logo_html = ""
    if logo:
        logo_html = "<div class=\"tlogo\"><img src=\"%s\"/></div>" % logo
    meta = "<div class=\"tname\">%s</div>" % _sce(name)
    if pick:
        meta += "<div class=\"tpick\">%s</div>" % _sce(pick)
    parts = ["<div class=\"trow\">",
             "<span class=\"tidx\">%d</span>" % i,
             logo_html,
             "<div class=\"tmeta\">%s</div>" % meta]
    if score:
        parts.append("<span class=\"tscore\">%s</span>" % _sce(score))
    if odds:
        parts.append("<span class=\"todds\">%s</span>" % _sce(odds))
    if ev:
        parts.append("<span class=\"tev %s\">%s</span>" % ("pos" if ev.startswith("+") else "", _sce(ev)))
    parts.append("</div>")
    return "".join(parts)


def _sce(x) -> str:
    return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _scorecard_build_html(*, c: dict, label: str, full_label: str, earl_uri: str,
                          lock_uri: str, kicker: str, title_html: str, wins_total: int,
                          dek_html: str, rows_html: str) -> str:
    css = _sc_css(c, _rgb(c["b0"]), _rgb(c["acc"]))
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>%s</style></head><body>"
        "<div class=\"card\">"
        "<div class=\"glow\"></div>"
        "<div class=\"earl\"><img src=\"%s\"/></div>"
        "<div class=\"inner\">"
        "<div class=\"topbar\"><img class=\"lockup\" src=\"%s\"/><span class=\"chip\">%s&nbsp;&bull;&nbsp;%s</span></div>"
        "<div class=\"head\">"
        "<div class=\"copy\"><span class=\"kicker\">%s</span><h1>%s</h1></div>"
        "%s"
        "</div>"
        "%s"
        "<div class=\"board\">%s</div>"
        "<div class=\"foot\"><span class=\"a\">EARL KNOWS BALL</span><span class=\"b\">%s</span></div>"
        "</div></div></body></html>"
    ) % (css, earl_uri, lock_uri, label, full_label,
         _sce(kicker), title_html, _sc_badge_html(wins_total, c["acc"], c["sub"]),
         dek_html, rows_html, full_label)


def generate_scorecard_card(
    *,
    sport: str,
    title: str,
    winners: Optional[list] = None,
    wins_total: Optional[int] = None,
    dek: str = "",
    accent: str = "",
    kicker: str = "EARL'S WINNERS",          # default eyebrow
    shirt: Optional[str] = None,             # desired Earl shirt color
    article_id: Optional[int] = None,
    out_png: pathlib.Path,
) -> str:
    """Render the SCORECARD (template B) card to ``out_png``.

    ``winners`` is an ordered list of dicts (newest/best first), one per pick that
    cashed on Earl's card. Recognized per-row keys (all optional except abbr):
        abbr  team abbreviation -> logo_uri(sport)         e.g. "LAA"
        sport league                                          "mlb"|"nfl"|"nba"|"all"
        name  display name                                    "Angels"
        pick  the exact coded pick                            "LAA +1.5"
        score final W-L score                                 "4-3"
        odds  odds as tipped                                  "-110"
        ev    EV on $100 as str                               "+0.55"
    ``wins_total`` overrides the badge count (defaults to len(winners)) for when
    a recap says "8 winners" but only some are shown or carried a score worth a row.
    Returns the /writeups/cards/<sport>/<file> web path. ``out_png`` parent must
    already exist (same contract as generate_social_card).
    """
    sport = sport if sport in SCHEMES else "all"
    wins = winners or []
    first_abbr = (wins[0].get("abbr") if wins else None)
    color = shirt if (shirt and shirt in THEME and shirt in _shirt_colors(sport)) \
        else pick_shirt(sport, first_abbr, article_id)
    c = _theme_for(sport, color)

    staged = _portrait_uri(sport, color)
    earl_uri = staged
    if not earl_uri:
        for cand in (ASSETS_DIR / "earl-original-card.png", ASSETS_DIR / "earl.png"):
            if cand.exists():
                earl_uri = _load_uri(cand, "image/png")
                break
    lock_path = ASSETS_DIR / "logo_lockup.png"
    lock_uri = _load_uri(lock_path, "image/png") if lock_path.exists() else ""

    title_html = accented_title(title, accent)
    total = wins_total if wins_total is not None else len(wins)
    dek_html = ("<p class=\"dek\">%s</p>" % _sce(dek)) if (dek or "").strip() else ""
    rows_html = "".join(_sc_row_html(i, w) for i, w in enumerate(wins, 1)) or \
        ("<div class=\"trow\"><div class=\"tmeta\"><div class=\"tpick\">"
         "No winners loaded — see the write-up.</div></div></div>")

    html = _scorecard_build_html(
        c=c, label=c["label"], full_label=c["full"],
        earl_uri=earl_uri, lock_uri=lock_uri,
        kicker=kicker, title_html=title_html, wins_total=total,
        dek_html=dek_html, rows_html=rows_html,
    )
    _screenshot(html, out_png)
    return "/writeups/cards/%s/%s" % (sport, out_png.name)


def compute_scorecard_path(*, article_id, sport="all") -> pathlib.Path:
    d = _CARDS_DIR / sport
    d.mkdir(parents=True, exist_ok=True)
    return d / ("original-scorecard-%d.png" % article_id)


# ---------------------------------------------------------------------------
# TEMPLATE C — "White Paper / Ledger" social card for Earl's Winners recaps.
#
# A clean paper-backed recap board: no dark gradient, no Earl portrait, no
# currency glyphs. The sport only lends one restrained accent (SCHEMES["acc"]),
# used for the kicker, the headline <em>, the top hairline and the +EV cells —
# everything else keeps to warm-white paper + near-black ink + hairline rules.
#
# Winner-dict contract per row (superset of the dark scoreboard in
# generate_scorecard_card). All keys optional except abbr:
#
#     abbr        team abbreviation  -> logos/<sport>/<ABBR>.png     e.g. "LAA"
#     sport       "mlb"|"nfl"|"nba"|"all"  (for logo lookup)
#     name        team display name                                  "Angels"
#     opponent    "vs Athletics" (prepended to the coded pick line)
#     market      SPREAD | TOTAL | MONEY LINE  (label only)
#     pick        the exact coded pick/line                           "LAA +1.5"
#                 ...any numeric token in pick (e.g. "+1.5","8.5","-110") may be
#                 surfaced in the LINES cell unless `spread` is given explicitly.
#     card        (optional) overrides the whole "card" column cell.
#     spread      (optional) explicit side line for its own LINES cell.
#     score       final score string                                  "6-4"
#     result      Covered | On the nose / Push | Lost  (default "Covered")
#     ev          EV on $100 as float or str          +0.55  -> rendered "+0.55"
#     note        short grey editorial tag                           "one-run game"
#
# Any row missing score/ev/note/spread simply omits that cell, so a recap which
# only carries the coded pick still renders a tidy paper board. No "$"/"USD"
# glyph is ever emitted.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TEMPLATE C — "White Paper / Ledger" social card, used ONLY for the Earl's
# Winners section (original_articles.section == "earls_winners").
#
# Design decisions locked with Rich (2026-09-08):
#   1. This card is the social card for Earl's Winners articles only
#      (discriminator: section == "earls_winners").
#   2. One FIXED brand accent (indigo-violet #4f46e5) for every Winners card —
#      no per-sport colors.
#   3. Winners only: the feature never renders a loss, so rows carry no
#      Win/Loss status — each listed bet is a hit, confirmed negative-free.
#   4. Every row shows BOTH teams in the game (two badges + names) so a total,
#      run-line or money line all read as a real matchup, not a lone favorite.
#
# It is intentionally money-currency-free: EV is shown as the raw signed figure
# (e.g. "+17.97"), never as "$"/"USD".
#
# Winner-dict contract per row:
#     sport     "mlb"|"nfl"|"nba"|"all"  (logo lookup only)
#     abbr_a / abbr_b   the two clubs' abbreviations (badge lookup). a=visitor, b=home.
#     name_a / name_b   friendly club names, e.g. "Athletics" / "Blue Jays"
#     market    SPREAD | TOTAL | MONEY LINE | RUN LINE  (optional label)
#     pick      the exact coded pick/line, e.g. "OAK +1.5", "Under 8.5", "NYM ML"
#     score     final score, e.g. "5-6"
#     ev        EV on $100 (float or str)  -> rendered "+17.97" green (no $)
#     note      short grey editorial tag    (optional)
#
# Any row missing score/ev/note simply omits that cell. A total has no club to
# favor on pick; both clubs are still shown and pick reads "Over 7.5".
# ---------------------------------------------------------------------------


# One fixed brand accent across every Earl's Winners card (Rich spec #2).
_WP_ACCENT_HEX = "#4f46e5"


def _winners_paper_ev_cell(w: dict) -> str:
    """EV -> signed raw figure, green. No '$'. Empty cell if absent."""
    ev = w.get("ev")
    if ev is None or ev == "":
        return '<span class="tev"></span>'
    try:
        f = float(ev)
        txt = ("+%.2f" % f) if f >= 0 else ("%.2f" % f)
        cls = "pos" if f >= 0 else "neg"
    except (TypeError, ValueError):
        txt = str(ev)
        cls = "pos" if txt.lstrip().startswith("+") else "neg"
    return '<span class="tev %s">%s</span>' % (cls, _sce(txt))


def _wp_club(abbr: Optional[str], sport: str) -> str:
    """A small round club badge; empty span when no abbr/logo available."""
    if not abbr:
        return '<span class="dot"></span>'
    lg = logo_uri(abbr, sport)
    if lg:
        return '<span class="dot"><img src="%s"/></span>' % lg
    return '<span class="dot">%s</span>' % _sce(abbr[:2])


def _sc_row_html2(i: int, w: dict) -> str:
    """One winners row: both teams + coded pick, then line/final/ev/note cells."""
    sport = w.get("sport") or "all"
    abbr_a = (w.get("abbr_a") or w.get("away") or "").strip()
    abbr_b = (w.get("abbr_b") or w.get("home") or "").strip()
    name_a = (w.get("name_a") or w.get("away_name") or w.get("away") or "").strip()
    name_b = (w.get("name_b") or w.get("home_name") or w.get("home") or "").strip()

    badges = _wp_club(abbr_a or None, sport) + _wp_club(abbr_b or None, sport)
    teams = '<div class="gteams">%s<span class="gsep"> at </span>%s</div>' % (
        _sce(name_a) if name_a else "&nbsp;", _sce(name_b) if name_b else "&nbsp;")

    opp = (w.get("opponent") or "").strip()
    market = (w.get("market") or "").strip()
    pick = (w.get("pick") or "").strip()
    card = (w.get("card") or "").strip()
    if not card:
        bits = []
        if market:
            bits.append(market.upper())
        if pick:
            bits.append(pick)
        card = " · ".join(bits)
    pick_html = ('<div class="vpick">%s</div>' % _sce(card)) if card else ""

    score = (w.get("score") or "").strip()
    score_html = ('<span class="tscore">%s</span>' % _sce(score)) if score else \
        '<span class="tscore"></span>'
    ev_html = _winners_paper_ev_cell(w)

    # DATE cell (retitled NOTES). American format MM/DD/YYYY when a real date is
    # given (ISO), else fall back to whatever non-empty note text was provided.
    date_raw = (w.get("date") or "").strip()
    if date_raw and len(date_raw) >= 10 and date_raw[4] == '-' and date_raw[7] == '-':
        mo, dd, yy = int(date_raw[5:7]), int(date_raw[8:10]), int(date_raw[0:4])
        date_txt = "%02d/%02d/%04d" % (mo, dd, yy) if yy else date_raw
    else:
        date_txt = (w.get("note") or "").strip()
    date_html = ('<span class="tdate">%s</span>' % _sce(date_txt)) if date_txt else \
        '<span class="tdate"></span>'

    return (
        '<div class="trow">'
        '<div class="clubs">%s</div>'
        '<div class="gmeta">%s%s</div>'
        '%s%s%s'
        '</div>'
    ) % (badges, teams, pick_html, score_html, ev_html, date_html)


def _winners_paper_css() -> str:
    """White-paper ledger CSS with the fixed Winners accent."""
    acc = "rgb(%s)" % _rgb(_WP_ACCENT_HEX)
    return f"""
    *{{margin:0;padding:0;box-sizing:border-box}}
    html,body{{width:1600px;height:900px;background:#ffffff;overflow:hidden}}
    .card{{position:relative;width:1600px;height:900px;overflow:hidden;color:#1b1b1b;
      background:#fdfdf8;font-family:Arial,'Segoe UI',Roboto,Helvetica,sans-serif}}
    .paperline{{position:absolute;left:44px;right:44px;top:0;bottom:0;
      border-left:1px solid #ece8dc;border-right:1px solid #ece8dc;pointer-events:none}}
    .inner{{position:relative;z-index:5;height:100%;padding:36px 96px 26px 96px;
      display:flex;flex-direction:column}}
    .topline{{height:5px;width:100%;background:{acc};margin-bottom:18px}}
    .topbar{{display:flex;align-items:center;justify-content:space-between}}
    .lockup{{height:46px;display:block}}
    .brandln{{font-size:16px;font-weight:900;letter-spacing:5px;color:#181818}}
    .chip{{font-size:12px;font-weight:800;letter-spacing:2px;color:#5a564a;
      border:1px solid #d6d0c2;padding:6px 15px;border-radius:40px;text-transform:uppercase}}
    .rule{{height:1px;background:#ddd8ca;margin:14px 0 18px}}
    .head{{display:flex;align-items:flex-end;gap:30px;flex:0 0 auto}}
    .copy{{flex:1 1 auto;min-width:0}}
    .kicker{{color:{acc};font-size:14px;font-weight:800;letter-spacing:3px;
      text-transform:uppercase;margin-bottom:7px}}
    .headline{{font-family:Georgia,'Times New Roman',Palatino,serif;font-weight:900;
      font-size:49px;line-height:1.03;letter-spacing:-.5px;color:#131313;margin:0;
      max-width:1060px}}
    .headline em{{color:{acc};font-style:normal}}
    .dek{{margin-top:9px;font-family:Georgia,Palatino,serif;font-style:italic;
      color:#4c4a40;font-size:16px;line-height:1.42;max-width:1060px}}
    .medallion{{flex:0 0 auto;width:138px;height:138px;border-radius:50%;margin-bottom:6px;
      border:3px solid #d8d3c6;background:#fff;display:flex;flex-direction:column;
      align-items:center;justify-content:center;box-shadow:inset 0 0 0 1px #efece0}}
    .med-bnum{{font-family:Georgia,serif;font-size:50px;font-weight:900;line-height:.85;
      color:{acc}}}
    .med-blab{{font-size:10px;font-weight:800;letter-spacing:1.4px;color:#8a8678;
      text-transform:uppercase;text-align:center;margin-top:6px;line-height:1.35}}
    .board{{flex:1 1 auto;display:flex;flex-direction:column;margin-top:16px;min-height:0}}
    .bhead,.trow{{display:grid;
      grid-template-columns:150px minmax(0,1fr) 160px 140px 96px;
      column-gap:16px;align-items:center}}
    .bhead{{font-size:10.5px;font-weight:800;letter-spacing:1.3px;color:#a6a192;
      text-transform:uppercase;padding:0 20px 7px 20px;border-bottom:1px solid #e3ddd0}}
    .bhead .ac{{text-align:center}}.bhead .ar{{text-align:right}}
    .trow{{background:#fff;border:1px solid #e1dccd;border-top:none;padding:15px 24px}}
    .trow.odd{{background:#faf8f1}}
    .trow:last-of-type{{border-radius:0 0 4px 4px}}
    .clubs{{display:flex;align-items:center;gap:10px}}
    .dot{{width:56px;height:56px;border-radius:50%;background:#efece0;border:1px solid #e5e0d2;
      display:flex;align-items:center;justify-content:center;font-family:Arial,sans-serif;
      font-size:11px;font-weight:900;color:#8a8678;overflow:hidden}}
    .dot img{{width:40px;height:40px;object-fit:contain}}
    .gmeta{{display:flex;flex-direction:column;min-width:0}}
    .gteams{{font-weight:800;font-size:26px;color:#161616;white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis;letter-spacing:.2px}}
    .gteams .gsep{{color:#c6c1b2;font-weight:700;margin:0 9px;font-size:17px;
      text-transform:uppercase}}
    .vpick{{font-size:18px;font-weight:700;color:{acc};letter-spacing:.2px;margin-top:4px;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .tscore{{text-align:center;font-weight:800;font-size:23px;color:#161616}}
    .tev{{text-align:right;font-weight:900;font-size:22px}}
    .tev.pos{{color:#14793f}}.tev.neg{{color:#b03a2e}}
    .tdate{{text-align:right;font-size:16px;font-weight:700;color:#8a8577;letter-spacing:.4px}}
    .empty-row{{padding:20px 22px;color:#6a6659;font-size:16px;font-style:italic}}
    .foot{{display:flex;align-items:center;justify-content:space-between;
      border-top:1px solid #ddd8ca;padding-top:11px;flex:0 0 auto}}
    .foot .a{{font-weight:800;letter-spacing:3px;font-size:13px}}
    .foot .c{{font-weight:700;letter-spacing:1.5px;font-size:11px;color:#8a8677}}
    """


def generate_winners_paper_card(
    *,
    sport: str,
    title: str,
    dek: str = "",
    accent: str = "",          # optional substring to highlight (em) in headline
    kicker: str = "EARL'S WINNERS",
    league_chip: str = "",      # e.g. "SUNDAY · SCOREBOARD"  (top-right pill)
    badge_count: int = 0,       # medallion number (defaults to len(winners))
    badge_label: str = "Top Winners",
    winners: list,
    article_id: Optional[int] = None,
    out_png: pathlib.Path,
) -> str:
    """Render the white-paper Earl's Winners recap card to out_png.

    TEMPLATE C — fixed indigo Winners accent; winners only (no losses); every
    row shows both teams; EV shown as a signed raw figure with no currency
    glyph. Returns the web path "/writeups/cards/<sport>/<file>"; out_png's
    parent dir must already exist (same contract as generate_social_card).
    """
    sport = sport if sport in SCHEMES else "all"
    wins = winners or []
    total = badge_count if badge_count else len(wins)

    title_html = accented_title(title, accent)
    title_html = title_html.replace("'", "&#39;").replace('"', "&quot;")
    dek_html = ('<p class="dek">%s</p>' % _sce(dek)) if (dek or "").strip() else ""

    rows_html = "".join(_sc_row_html2(i, w) for i, w in enumerate(wins, 1))
    if not rows_html:
        rows_html = ('<div class="trow empty-row">No winners recorded for this '
                     'recap window.</div>')

    css = _winners_paper_css()
    chip_html = ('<span class="chip">%s</span>' % _sce(league_chip)) if \
        (league_chip or "").strip() else ""

    lock_path = ASSETS_DIR / "logo_lockup.png"
    lock_uri = _load_uri(lock_path, "image/png") if lock_path.exists() else ""
    brand_html = ('<img class="lockup" src="%s"/>' % lock_uri) if lock_uri else \
        '<span class="brandln">EARL KNOWS BALL</span>'

    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>%s</style>"
        "</head><body>"
        "<div class=\"card\">"
        "<div class=\"paperline\"></div>"
        "<div class=\"inner\">"
        "<div class=\"topline\"></div>"
        "<div class=\"topbar\">%s%s</div>"
        "<div class=\"rule\"></div>"
        "<div class=\"head\">"
        "<div class=\"copy\">"
        "<div class=\"kicker\">%s</div>"
        "<h1 class=\"headline\">%s</h1>%s</div>"
        "<div class=\"medallion\"><div class=\"med-bnum\">%d</div>"
        "<div class=\"med-blab\">%s</div></div>"
        "</div>"
        "<div class=\"board\">"
        "<div class=\"bhead\">"
        "<div>&nbsp;</div><div>GAME</div>"
        "<div class=\"ac\">FINAL</div>"
        "<div class=\"ar\">EV</div><div class=\"ar\">DATE</div>"
        "</div>"
        "%s"
        "</div>"
        "<div class=\"foot\"><span class=\"a\">EARL KNOWS BALL</span>"
        "<span class=\"c\">earlknowsball.com</span></div>"
        "</div></div></body></html>"
    ) % (css, brand_html, chip_html,
         _sce(kicker), title_html, dek_html,
         int(total), _sce(badge_label),
         rows_html)

    _screenshot(html, out_png)
    return "/writeups/cards/%s/%s" % (sport, out_png.name)
