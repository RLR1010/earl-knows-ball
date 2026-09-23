"""
Earl Power Rankings -- weekly team blurbs.

Reads a nfl.power_ratings snapshot (season/week) and asks the LLM for a short
2-3 sentence, gambling-flavored take on each team, grounded ONLY in the numbers
(rank, rating, movement, SOS, offense/defense, EPA). Writes nfl.power_ranking_blurbs.

Run:  PYTHONPATH=. ./venv/bin/python -m app.ingestion.generate_power_ranking_blurbs 2026 2
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db_urls import ASYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("power_blurbs")

PROMPT_VERSION = "blurb-v0"
CONCURRENCY = 6

DDL = """
CREATE TABLE IF NOT EXISTS {schema}.power_ranking_blurbs (
    season        integer      NOT NULL,
    week          integer      NOT NULL,
    team_id       integer      NOT NULL,
    team_abbr     text,
    blurb         text,
    rating        double precision,
    rank          integer,
    model         text,
    prompt_version text,
    generated_at  timestamptz  DEFAULT now(),
    PRIMARY KEY (season, week, team_id)
);
"""

SPORTS = {
    "nfl": dict(league="NFL", unit="points"),
    "nba": dict(league="NBA", unit="points"),
    "mlb": dict(league="MLB", unit="runs"),
}

SYSTEM_TMPL = (
    "You are Earl, a sharp, data-driven {league} handicapper. For the team provided, write "
    "EXACTLY 2-3 sentences. Be confident and specific, and ground every claim in the "
    "numbers given (do not invent stats). Include a gambling angle where it fits: is the "
    "team overrated/underrated, a buy/sell, or how the rating compares to a typical spread. "
    "Plain text only -- no heading, no team name prefix, no markdown, no bullet points."
)

SYSTEM = SYSTEM_TMPL.format(league="NFL")   # default (NFL) for backward compatibility
_UNIT = "points"


def build_user(row):
    c = row["components"] or {}
    if isinstance(c, (str, bytes)):
        try:
            c = json.loads(c)
        except Exception:
            c = {}
    inj = c.get("injuries") or []
    d = {
        "team": row["team_abbr"],
        "rank": row["rank"],
        "rating_" + _UNIT + "_neutral_field": row["rating"],
        "rating_change_vs_last_week": row["rating_delta"],
        "strength_of_schedule": row["sos"],
        "prior_season_rating": c.get("prior_rating"),
        "off_ppg_last5": c.get("off_ppg_r5"),
        "def_ppg_allowed_last5": c.get("def_ppg_r5"),
        "epa_diff_per_play_last5": c.get("epa_diff_r5"),
        "point_diff_last5": c.get("point_diff_r5"),
        "injury_points_docked": c.get("injury_adj"),
        "wins": row.get("wins"), "losses": row.get("losses"),
        "vs_market": c.get("market_delta"),
        "key_players_unavailable": [
            f"{p.get('name')} ({p.get('position')}, "
            f"{p.get('report_status') or p.get('practice_status') or '?'})"
            for p in inj[:3]
            if p.get("name")
        ],
        "note": "rating is " + _UNIT + " better than an average team on a neutral field; "
                "rating minus opponent rating approximates the spread. The rating "
                "ALREADY includes a dock for unavailable key players "
                "(injury_points_docked); mention notable absences when they are material "
                "to the team's outlook, but do not invent injuries not listed.",
    }
    return f"Team data (JSON):\n{json.dumps(d)}\n\nWrite Earl's 2-3 sentence take."


async def gen_one(client, model, sem, row, out, system=None):
    async with sem:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system or SYSTEM},
                      {"role": "user", "content": build_user(row)}],
            temperature=0.6,
            max_tokens=1500,
            extra_body={"thinking": {"type": "disabled"}},
        )
        txt = (r.choices[0].message.content or "").strip()
        out.append({"row": row, "blurb": txt, "model": model})


async def main(season, week, sport: str = "nfl"):
    global SYSTEM, _UNIT
    cfg = SPORTS.get(sport, SPORTS["nfl"])
    _UNIT = cfg["unit"]
    system = SYSTEM_TMPL.format(league=cfg["league"])
    SYSTEM = system
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        await db.execute(text(DDL.format(schema=sport)))
        rows = [dict(r._mapping) for r in (await db.execute(text(f"""
            SELECT team_id, team_abbr, rating, rank, rating_delta, sos,
                   wins, losses, components
            FROM {sport}.power_ratings WHERE season = :s AND week = :w ORDER BY rank
        """), {"s": season, "w": week})).all()]
        if not rows:
            logger.error("no snapshot for %s %s week %s", sport, season, week)
            return

        client = AsyncOpenAI(api_key=settings.deepseek_api_key,
                             base_url=f"{settings.deepseek_base_url}/v1")
        sem = asyncio.Semaphore(CONCURRENCY)
        out = []
        await asyncio.gather(*[gen_one(client, settings.deepseek_model, sem, row, out, system)
                               for row in rows])
        await client.close()

        payload = [{
            "season": season, "week": week, "team_id": o["row"]["team_id"],
            "team_abbr": o["row"]["team_abbr"], "blurb": o["blurb"],
            "rating": o["row"]["rating"], "rank": o["row"]["rank"],
            "model": o["model"], "prompt_version": PROMPT_VERSION,
        } for o in out if o["blurb"]]

        await db.execute(text(f"""
            INSERT INTO {sport}.power_ranking_blurbs
              (season, week, team_id, team_abbr, blurb, rating, rank, model, prompt_version)
            VALUES (:season,:week,:team_id,:team_abbr,:blurb,:rating,:rank,:model,:prompt_version)
            ON CONFLICT (season, week, team_id) DO UPDATE SET
              blurb=EXCLUDED.blurb, rating=EXCLUDED.rating, rank=EXCLUDED.rank,
              model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version, generated_at=now()
        """), payload)
        await db.commit()

        out.sort(key=lambda o: o["row"]["rank"])
        print(f"\n===== SAMPLE BLURBS ({sport} {season} week {week}) =====")
        for o in [out[0], out[len(out)//2], out[-1]]:
            print(f"\n#{o['row']['rank']} {o['row']['team_abbr']} "
                  f"({o['row']['rating']:+.2f}):\n{o['blurb']}")
        logger.info("wrote %d blurbs", len(payload))

    await engine.dispose()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in SPORTS:
        asyncio.run(main(int(args[1]), int(args[2]), args[0]))
    else:
        asyncio.run(main(int(args[0]), int(args[1])))
