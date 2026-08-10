"""Backfill: merge the props-research context into existing MLB game_writeups rows.

Reconstructs what _generate_props_article now sends to the LLM (prop odds +
per-player season/recent stats) and merges it under research_brief['prop_research']
for rows that have a prop article but were generated before this field existed.

Usage: python app/scripts/backfill_props_research.py --sport mlb [--game-id 48858]
"""
import argparse
import asyncio
import json
import sys

sys.path.insert(0, "backend")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.writeups import props_article as shared
from app.writeups import props_mlb as mlb


async def backfill_game(db, sport: str, game_id: int) -> bool:
    cfg = shared.SPORT_CONFIGS.get(sport)
    if not cfg:
        print(f"  ! unknown sport {sport}"); return False
    cfg = cfg()
    schema = cfg["schema"]

    row = (await db.execute(
        text(f"SELECT research_brief, prop_content FROM {schema}.game_writeups WHERE game_id = :gid"),
        {"gid": int(game_id)},
    )).mappings().first()
    if not row or not row["prop_content"]:
        return False

    existing = {}
    if row["research_brief"]:
        raw = row["research_brief"]
        existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(existing, dict):
            existing = {}
    if "prop_research" in existing:
        print(f"  game {game_id} already has prop_research — skipping"); return False

    props = await shared.fetch_game_props(db, cfg, game_id)
    if not props:
        print(f"  game {game_id} has no props — skipping"); return False

    prop_players = mlb.extract_prop_players(props)
    season_lookup = mlb.build_season_lookup(existing)
    player_context = []
    for name, team_id in prop_players.items():
        season = season_lookup.get(mlb._norm(name))
        recent = None
        player_id = season.get("player_id") if season else None
        if player_id is None:
            try:
                player_id = await mlb.resolve_player_id(db, name, team_id)
            except Exception:  # noqa: BLE001
                player_id = None
        if player_id:
            try:
                lines = await mlb.fetch_player_recent_stats(db, player_id)
                if lines:
                    recent = {"last_n": len(lines), "lines": lines}
            except Exception:  # noqa: BLE001
                recent = None
        player_context.append({"name": name, "season": season, "recent": recent, "team_id": team_id})

    props_for_llm = props[: shared.MAX_PROPS_TO_SEND] if len(props) > shared.MAX_PROPS_TO_SEND else props
    prop_research = {
        "game_id": int(game_id),
        "prop_count": len(props),
        "prop_lines_sent": len(props_for_llm),
        "props": props,
        "players": player_context,
    }

    existing["prop_research"] = prop_research
    await db.execute(
        text(f"UPDATE {schema}.game_writeups SET research_brief = :rb WHERE game_id = :gid"),
        {"rb": json.dumps(existing, default=str), "gid": int(game_id)},
    )
    print(f"  game {game_id}: merged prop_research ({len(props)} props, {len(player_context)} players)")
    return True


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="mlb", choices=["mlb", "nfl", "nba"])
    ap.add_argument("--game-id", type=int, default=None)
    args = ap.parse_args()

    engine = create_async_engine(settings.database_url)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        if args.game_id:
            if await backfill_game(db, args.sport, args.game_id):
                await db.commit()
        else:
            rows = (await db.execute(
                text(
                    f"SELECT game_id FROM {args.sport}.game_writeups "
                    f"WHERE prop_content IS NOT NULL ORDER BY prop_published_at DESC"
                )
            )).scalars().all()
            done = 0
            for gid in rows:
                try:
                    if await backfill_game(db, args.sport, int(gid)):
                        done += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  ! game {gid} error: {e}")
            await db.commit()
            print(f"backfilled {done}/{len(rows)} games")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
