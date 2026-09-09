"""Bespoke promotional social card for the "Free Pick" giveaway.

Unlike the standard public game-writeup card (which renders the matchup + stat windows and is
stored in `preview_image`), this is a premium, giveaway-focused card: it stresses "FREE PICKS
from Earl" as the hero message while still anchoring on the real matchup. It is built ONLY when
an admin hits "Feature" in /admin/free-pick and is stored in the separate `premium_social_card`
column (rel path `/writeups/cards/<sport>/free-<game_id>.png`).

Rendering reuses the project's proven HTML->PNG pipeline (`cards.render_png`, Playwright) at
1600x900, and pulls the matchup via each sport's existing resolved-team-card helper so the art
stays consistent with the site (team logos, records, Earl portrait).
"""
from __future__ import annotations

import pathlib

from . import cards as _cards
from . import cards_nfl as _nfl
from . import cards_nba as _nba

# name -> (team-card resolver fn, earl portrait, sport accent css color, accent2)
_SPORT = {
    "mlb": (_cards.game_team_cards, _cards.EARL_PNG, "#d71920", "#ffd24a"),
    "nfl": (_nfl.nfl_team_cards, _nfl.EARL_NFL, "#00e0a0", "#ffd24a"),
    "nba": (_nba.nba_team_cards, _nba.EARL_NBA, "#ff7b00", "#ffd24a"),
}
_ASSETS = pathlib.Path(__file__).resolve().parent / "assets"


def _data_uri(path: pathlib.Path) -> str:
    import base64, mimetypes
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def _fill(template: str, **kw):
    for k, v in kw.items():
        template = template.replace("{{" + k + "}}", v or "")
    return template


def _free_pick_html(
    sport: str,
    away: dict,
    home: dict,
    title: str,
    dek: str,
    slug: str,
    accent: str,
    accent2: str,
    earl_uri: str,
    logo_uri: str,
) -> str:
    away_logo = away.get("logo_url") or ""
    home_logo = home.get("logo_url") or ""
    away_name = (away.get("name") or "").upper()
    home_name = (home.get("name") or "").upper()
    away_meta = (away.get("meta") or "").split("·")[0].strip()
    home_meta = (home.get("meta") or "").split("·")[0].strip()
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    title = esc(title)[:110]
    dek = esc(dek)[:140]
    url_path = f"{sport}/analysis/{slug}" if slug else "#"

    # ---- precompute conditional fragments as true f-strings (NO mid-string concat leaks) ----
    logo_html = (
        f"<img src=\"{logo_uri}\" style=\"height:40px;width:auto;object-fit:contain;\" />"
        if logo_uri
        else "<span style=\"font-size:17px;font-weight:900;letter-spacing:3px;color:#e8edf4;text-transform:uppercase;\">Earl&nbsp;Knows&nbsp;Ball</span>"
    )
    if away_logo:
        away_logo_html = (
            f"<div style=\"width:210px;height:210px;border-radius:50%;margin:0 auto;background:linear-gradient(180deg,#ffffff,#e6ebf2);display:flex;align-items:center;justify-content:center;box-shadow:0 10px 34px rgba(0,0,0,.45);\">"
            f"<img src=\"{away_logo}\" style=\"width:120px;height:auto;\" /></div>"
        )
    else:
        away_logo_html = f"<div style=\"width:210px;height:210px;margin:0 auto;\"></div>"
    if home_logo:
        home_logo_html = (
            f"<div style=\"width:210px;height:210px;border-radius:50%;margin:0 auto;background:linear-gradient(180deg,#ffffff,#e6ebf2);display:flex;align-items:center;justify-content:center;box-shadow:0 10px 34px rgba(0,0,0,.45);\">"
            f"<img src=\"{home_logo}\" style=\"width:120px;height:auto;\" /></div>"
        )
    else:
        home_logo_html = f"<div style=\"width:210px;height:210px;margin:0 auto;\"></div>"
    dek_html = (
        f"<div style=\"color:#c7cdd6;font-size:19px;margin-top:12px;\">{dek}</div>" if dek else ""
    )

    return f"""<!doctype html><html><head><meta charset="utf-8"/></head><body style="margin:0">
<div style="width:1600px;height:900px;position:relative;overflow:hidden;
     font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;background:#0a0d12;color:#fff;">
  <div style="position:absolute;inset:0;background:
       radial-gradient(1200px 500px at 20% -10%, {accent}26 0%, transparent 60%),
       radial-gradient(900px 500px at 110% 120%, {accent2}1f 0%, transparent 60%);"></div>

  <!-- top band -->
  <div style="position:absolute;top:0;left:0;right:0;height:9px;background:linear-gradient(90deg,{accent},{accent2});"></div>
  <div style="display:flex;align-items:center;justify-content:space-between;padding:26px 46px 0;">
    <div style="display:flex;align-items:center;">{logo_html}</div>
    <div style="background:{accent};color:#fff;font-weight:800;font-size:16px;letter-spacing:2px;
         padding:9px 20px;border-radius:40px;box-shadow:0 4px 22px {accent}66;">FREE PICKS</div>
  </div>

  <!-- matchup -->
  <div style="display:flex;align-items:center;justify-content:center;gap:40px;margin-top:34px;">
    <div style="text-align:center;width:350px;">
      {away_logo_html}
      <div style="font-size:27px;font-weight:800;letter-spacing:1px;margin-top:16px;">{away_name}</div>
      <div style="font-size:16px;color:#aab2bd;margin-top:5px;">{away_meta}</div>
    </div>
    <div style="font-size:50px;font-weight:900;color:{accent2};letter-spacing:4px;">VS</div>
    <div style="text-align:center;width:350px;">
      {home_logo_html}
      <div style="font-size:27px;font-weight:800;letter-spacing:1px;margin-top:16px;">{home_name}</div>
      <div style="font-size:16px;color:#aab2bd;margin-top:5px;">{home_meta}</div>
    </div>
  </div>

  <!-- giveaway headline -->
  <div style="text-align:center;margin-top:44px;padding:0 100px;">
    <div style="font-size:32px;font-weight:900;line-height:1.28;color:#fff;text-transform:uppercase;letter-spacing:1px;">
      Earl&rsquo;s full premium breakdown<br/>is yours &mdash; <span style="color:{accent2};">free</span>
    </div>
    <div style="font-size:33px;font-weight:800;color:#fff;margin-top:12px;line-height:1.2;">{title}</div>
    {dek_html}
    <div style="display:inline-flex;align-items:center;gap:10px;margin-top:22px;background:{accent2};
         color:#10141b;font-weight:900;font-size:17px;letter-spacing:1px;padding:11px 24px;border-radius:10px;
         box-shadow:0 6px 24px {accent2}55;">LOCKED FOR PREMIUM &middot; NOW <u>UNLOCKED</u> FOR YOU</div>
  </div>

  <!-- bottom bar with earl portrait + cta -->
  <div style="position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:space-between;
       padding:18px 46px 26px;background:#07090d; ">
    <div style="display:flex;align-items:center;gap:18px;">
      <div style="width:84px;height:84px;border-radius:50%;overflow:hidden;box-shadow:0 4px 18px rgba(0,0,0,.4);">
        <img src="{earl_uri}" style="width:100%;height:100%;object-fit:cover;object-position:50% 0%;display:block;" />
      </div>
      <div>
        <div style="font-weight:800;font-size:18px;">Earl&rsquo;s Free Pick</div>
        <div style="font-size:14px;color:#9aa3ad;">Full breakdown, zero paywall.</div>
      </div>
    </div>
    <div style="font-size:17px;font-weight:800;color:#e8edf4;letter-spacing:1px;">earlknowsball.com</div>
  </div>
</div></body></html>"""


def render_free_pick_card(sport: str, game_id: int, slug: str = "", conn_or_engine=None):
    """Render + write the Free Pick promo card PNG. Returns served rel path
    ('/writeups/cards/<sport>/free-<game_id>.png') — callers store it in premium_social_card.

    Reuses the sport's existing resolved-team-card helper for a trustworthy matchup, but builds
    an entirely promotional HTML layout (distinct from the public/gw- card). ``slug`` is the
    writeup slug (resolvers don't expose it) for the CTA url.
    """
    sport = sport.lower()
    if sport not in _SPORT:
        raise ValueError(f"no free-pick card config for sport {sport!r}")

    team_cards_fn, earl_path, accent, accent2 = _SPORT[sport]
    engine = conn_or_engine or _cards._engine()

    own = False
    conn = None
    from sqlalchemy.engine import Engine, Connection
    if isinstance(engine, Engine):
        conn = engine.connect()
        own = True
    elif isinstance(engine, Connection):
        conn = engine
    else:
        conn = None
    try:
        resolved = team_cards_fn(game_id, conn)
    finally:
        if own and conn is not None:
            conn.close()

    sport_dir = _cards.CARDS_DIR / sport
    sport_dir.mkdir(parents=True, exist_ok=True)
    rel = f"/writeups/cards/{sport}/free-{int(game_id)}.png"
    out = _cards.CARDS_DIR / sport / f"free-{int(game_id)}.png"

    html = _free_pick_html(
        sport,
        resolved.get("away", {}),
        resolved.get("home", {}),
        resolved.get("title", ""),
        resolved.get("dek", ""),
        slug or "",
        accent,
        accent2,
        _data_uri(earl_path),
        _data_uri(_ASSETS / "logo_lockup.png") if (_ASSETS / "logo_lockup.png").exists() else "",
    )
    _cards.render_png(html, out)
    return {"premium_social_card": rel, "path": str(out), "away": resolved.get("away"), "home": resolved.get("home")}
