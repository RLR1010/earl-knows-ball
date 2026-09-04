"""
Generate a sample NFL social card (orange-Earl variant) for a real past-season game,
to review the look on DEV before wiring persistence/prod. Standalone; does NOT touch
the MLB card path in app/social/cards.py.

Usage:  PYTHONPATH=. ../venv/bin/python scripts/gen_nfl_card_sample.py  [game_id]
Example game: season_id=1 (2025-26), PHI @ BUF on 2025-12-28 (game found by abbr/date).
Writes PNG to frontend/public/nfl-card-sample.png so it shows on the dev site.
"""
from __future__ import annotations

import datetime as dt
import pathlib
from urllib.request import urlopen, Request

# ---- paths ----
ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "social" / "assets"
TEMPLATE = ROOT / "app" / "social" / "card_template.html"
# frontend public dir lives next to backend in the same repo
OUT = ROOT.parent / "frontend" / "public" / "nfl-card-sample.png"
OUT = OUT.resolve()

EARL_ORANGE = ASSETS / "earl-nfl.png"


def _data_uri(path: pathlib.Path, mime: str) -> str:
    import base64
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _logo_url(abbr: str) -> str:
    # Official NFL logos via ESPN CDN by abbreviation.
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"


def _fetch_data_uri(url: str) -> str:
    import base64
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        b = r.read()
    return "data:" + (r.headers.get_content_type() or "image/png") + ";base64," + base64.b64encode(b).decode()


def _db():
    import os, pathlib
    env = pathlib.Path(ROOT / ".env")
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    # sync engine; DATABASE_URL may be asyncpg driver
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql+asyncpg"):
        url = url.replace("postgresql+asyncpg", "postgresql+psycopg2")
    from sqlalchemy import create_engine, text
    return create_engine(url), text


def load_team(c, text, abbr: str, game_date: dt.date) -> dict:
    # rolling row through the last completed game before game_date (leak-safe)
    row = c.execute(
        text(
            """select team_abbr, game_date, season_wins, season_losses,
                     off_pts_r5, def_pts_r5, win_pct_r5, point_diff_r5
              from nfl.team_rolling_stats
              where team_abbr=:a and game_date < :gd
              order by game_date desc limit 1"""
        ),
        {"a": abbr, "gd": game_date},
    ).mappings().first()
    if not row:
        raise RuntimeError(f"no rolling row for {abbr} before {game_date}")
    tmeta = c.execute(
        text("select name, abbreviation, conference, division from nfl.teams where abbreviation=:a"),
        {"a": abbr},
    ).mappings().first()
    conf = (tmeta.get('conference') or '').upper()
    div = tmeta.get('division') or ''
    meta_label = f"{conf}-{div}" if conf and not div.lower().startswith(conf.lower()) else (conf or div or '')
    rec = f"{int(row['season_wins'])}-{int(row['season_losses'])}"
    meta = f"{rec} · {meta_label}"
    return {
        "abbr": abbr,
        "name": tmeta["name"],
        "meta": meta,
        # points are stored as per-game in the r5 window columns
        "rs5": f"{row['off_pts_r5']:.1f}",
        "ra5": f"{row['def_pts_r5']:.1f}",
        "avg10": f"{row['win_pct_r5'] * 100:.0f}%",
    }


def main() -> None:
    engine, text = _db()
    game_date = dt.date(2025, 12, 28)
    away_abbr, home_abbr = "PHI", "BUF"

    tmpl = TEMPLATE.read_text()
    html = tmpl

    # sport eyebrow
    html = html.replace("GAME WRITEUP · MLB · {DATE}", "GAME WRITEUP · NFL · {DATE}")

    # football stat labels (inject per-row) - replace MLB wording once (both cells use same words)
    html = html.replace("Runs Allowed/G Last 5", "Points Allowed/G Last 5")
    html = html.replace("Runs Scored/G Last 5", "Points Scored/G Last 5")
    html = html.replace("AVG Last 10", "Win % Last 5")

    # headline/dek from the matchup
    date_str = "December 28, 2025"
    with engine.connect() as conn:
        away = load_team(conn, text, away_abbr, game_date)
        home = load_team(conn, text, home_abbr, game_date)

    headline = "The Eagles meet the Bills in a week-17 heavyweight clash"
    dek = "A deep playoff-stakes matchup with two powerhouse rosters."

    earl = _data_uri(EARL_ORANGE, "image/png")
    lockup = _data_uri(ASSETS / "logo_lockup.png", "image/png")
    awaylogo = _fetch_data_uri(_logo_url(away["abbr"]))
    homelogo = _fetch_data_uri(_logo_url(home["abbr"]))

    repl = {
        "{EARL_SRC}": earl,
        "{DATE}": date_str,
        "{HEADLINE}": headline, "{DEK}": dek,
        "{AWAY_LOGO}": awaylogo, "{AWAY_NAME}": away["name"], "{AWAY_META}": away["meta"],
        "{AWAY_RA5}": away["ra5"], "{AWAY_RS5}": away["rs5"], "{AWAY_AVG10}": away["avg10"],
        "{HOME_LOGO}": homelogo, "{HOME_NAME}": home["name"], "{HOME_META}": home["meta"],
        "{HOME_RA5}": home["ra5"], "{HOME_RS5}": home["rs5"], "{HOME_AVG10}": home["avg10"],
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    html = html.replace("{LOGO_SRC}", lockup)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # render
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        pg.set_content(html, wait_until="load")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(OUT))
        b.close()
    print("WROTE", OUT)
    print("PHI", away["meta"], "PF5", away["rs5"], "PA5", away["ra5"])
    print("BUF", home["meta"], "PF5", home["rs5"], "PA5", home["ra5"])


if __name__ == "__main__":
    main()
