#!/usr/bin/env python3
"""Weekly Power Rankings social card (1600x900 PNG) — BIG-TYPE HOOK edition.

Optimised for X on a phone: the whole image is ~360px wide there, so only a few
short lines render legibly. Rules baked in:
  * NO team names and NO rankings on the card (that IS the content) — tease only.
  * Very few words, very large type (nothing below ~40px on a 1600px canvas).
  * A single clear CTA to the page.

Cards are written under ``backend/var/cards/<sport>/power-rankings-<season>-wk<week>.png``
and served live by the writeups ``/cards/{sport}/{filename}`` FileResponse route
(never rely on Next's static public/ — see TOOLS.md).

Usage:
    python -m app.social.power_rankings_card nfl 2026 2
"""
from __future__ import annotations

import asyncio
import html
import pathlib
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

ROOT = pathlib.Path(__file__).resolve().parents[2]          # .../backend
CARDS_DIR = ROOT / "var" / "cards"

SPORT_LABEL = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB"}
SPORT_TABLE = {"nfl": "nfl.power_ratings", "nba": "nba.power_ratings", "mlb": "mlb.power_ratings"}
SITE = "earlknowsball.com"


async def _fetch(sport: str, season: int, week: int) -> list[dict]:
    table = SPORT_TABLE.get(sport)
    if not table:
        raise ValueError(f"unsupported sport {sport!r}")
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            rows = (
                await db.execute(
                    text(
                        f"""
                        SELECT p.rank, p.rank_delta, p.market_delta, p.injury_adj
                        FROM {table} p
                        WHERE p.season = :s AND p.week = :w
                        ORDER BY p.rank
                        """
                    ),
                    {"s": season, "w": week},
                )
            ).all()
    finally:
        await engine.dispose()
    return [
        {"rank": r[0], "movement": -(r[1] or 0), "market_delta": r[2], "injury_adj": r[3]}
        for r in rows
    ]


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _hooks(board: list[dict]) -> list[str]:
    """A few SHORT, anonymous hook lines (numbers emphasised, no team names)."""
    out: list[str] = []
    top = board[0] if board else None
    if top and top["movement"] > 0:
        out.append("There's a <b>new No. 1</b>.")
    risers = sorted((t for t in board if t["movement"] > 0), key=lambda x: -x["movement"])
    if risers:
        mv = risers[0]["movement"]
        out.append(f"One team just jumped <b>{mv} spots</b>.")
    inj = max((t["injury_adj"] or 0 for t in board), default=0)
    if inj >= 3:
        out.append(f"A contender lost <b>{inj:.0f} rating points</b> to injuries.")
    edges = [t for t in board if t["market_delta"] is not None]
    n = len([t for t in edges if abs(t["market_delta"]) >= 3])
    if n:
        out.append(f"Earl disagrees with Vegas on <b>{n} teams</b>.")
    fallers = sorted((t for t in board if t["movement"] < 0), key=lambda x: x["movement"])
    if len(out) < 3 and fallers:
        out.append(f"A playoff team slid <b>{abs(fallers[0]['movement'])} spots</b>.")
    return out[:3]


def build_html(sport: str, season: int, week: int, board: list[dict]) -> str:
    label = SPORT_LABEL.get(sport, sport.upper())
    hooks = _hooks(board)
    page = f"{SITE}/{sport}/power-rankings"

    css = """
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#0a0f0d;font-family:'DejaVu Sans',Arial,Helvetica,sans-serif;color:#eafff6}
    .card{width:1600px;height:900px;padding:56px 84px;display:flex;flex-direction:column;
      justify-content:center;background:radial-gradient(1100px 640px at 78% -12%, #14432f 0%, #0a0f0d 62%)}
    .brand{font-size:44px;font-weight:800;letter-spacing:4px;color:#34d399;margin-bottom:20px}
    .title{font-size:112px;font-weight:800;letter-spacing:1px;line-height:1.02}
    .title em{color:#34d399;font-style:normal}
    .wk{font-size:60px;font-weight:700;color:#9dc7b6;margin-top:6px}
    .rule{height:3px;background:#1f5c43;margin:30px 0 30px}
    .hook{font-size:64px;font-weight:600;color:#eafff6;margin:16px 0;line-height:1.12}
    .hook b{color:#34d399;font-weight:800}
    .foot{display:flex;flex-direction:column;align-items:flex-start;gap:14px;margin-top:48px}
    .ctaline{font-size:46px;font-weight:600;color:#9dc7b6}
    .cta{background:#34d399;color:#06120c;font-weight:800;font-size:52px;border-radius:999px;
      padding:20px 40px;letter-spacing:1px;white-space:nowrap}
    """

    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + css
        + "</style></head><body><div class='card'>"
        + '<div class="brand">EARL KNOWS BALL</div>'
        + f"<div class='title'>{label} Power <em>Rankings</em></div>"
        + f"<div class='wk'>{season} &bull; Week {week}</div>"
        + "<div class='rule'></div>"
        + "".join(f"<div class='hook'>{h}</div>" for h in hooks)
        + "<div class='foot'><div class='ctaline'>Full board, ratings &amp; Earl&apos;s take &rarr;</div>"
        + f"<div class='cta'>{page}</div></div>"
        + "</div></body></html>"
    )


def _screenshot(html_text: str, out_png: pathlib.Path) -> None:
    from playwright.sync_api import sync_playwright

    out_png.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.set_content(html_text, wait_until="networkidle", timeout=40000)
        page.screenshot(path=str(out_png))
        browser.close()


async def _latest_week(sport: str, season: int) -> int | None:
    table = SPORT_TABLE[sport]
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            return await db.scalar(
                text(f"SELECT max(week) FROM {table} WHERE season = :s"), {"s": season}
            )
    finally:
        await engine.dispose()


def generate(sport: str = "nfl", season: int | None = None, week: int | None = None):
    """Render the card; returns the on-disk path (str) and public URL path."""
    if season is None:
        season = 2026
    if week is None:
        week = asyncio.run(_latest_week(sport, season))
    board = asyncio.run(_fetch(sport, season, week))
    if not board:
        raise RuntimeError(f"no power-ranking rows for {sport} {season} wk{week}")
    out_png = CARDS_DIR / sport / f"power-rankings-{season}-wk{week}.png"
    _screenshot(build_html(sport, season, week, board), out_png)
    return str(out_png), f"/writeups/cards/{sport}/{out_png.name}"


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    se = int(sys.argv[2]) if len(sys.argv) > 2 else None
    wk = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(generate(s, se, wk))
