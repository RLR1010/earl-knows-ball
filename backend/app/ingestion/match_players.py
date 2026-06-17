"""Match our DB players to nflverse GSIS IDs by name + position."""

import httpx
import io
import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Player


async def match_nflverse_ids(session: AsyncSession) -> dict:
    """Backfill nflverse_id for players who don't have it by matching on name + position."""
    # Download nflverse roster
    url = "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        roster_df = pd.read_parquet(buf)

    # Only active or relevant status players
    roster_df = roster_df[roster_df["status"].isin(["ACT", "RES", "DEV"])]
    # Only skill positions + K + DST-like
    roster_df = roster_df[roster_df["position"].isin(["QB", "RB", "WR", "TE", "K", "DB", "LB", "DL", "CB", "S", "DE", "DT", "OL", "NT", "LS", "P", "FB", "SS", "OLB", "ILB", "MLB"])]

    # Build lookup dicts for faster matching
    by_name = {}   # (display_name_lower, position) -> gsis_id
    by_short = {}  # (short_name_lower, position) -> gsis_id
    for _, row in roster_df.iterrows():
        gsis = row.get("gsis_id")
        if not gsis:
            continue
        name = str(row.get("display_name", "")).strip().lower()
        pos = row.get("position", "")
        short = str(row.get("short_name", "")).strip().lower()

        if name and pos:
            key = (name, pos)
            if key not in by_name:
                by_name[key] = gsis
        if short and pos and short != "nan":
            key = (short, pos)
            if key not in by_short:
                by_short[key] = gsis

    # Get all our players missing nflverse_id
    result = await session.execute(
        select(Player).where(Player.nflverse_id.is_(None))
    )
    missing = result.scalars().all()
    print(f"  {len(missing)} players missing nflverse_id")

    matched = 0
    name_matched = 0
    short_matched = 0

    for player in missing:
        if not player.name or not player.position:
            continue

        player_name = player.name.strip().lower()
        player_pos = player.position.upper()

        # Try exact name match
        gsis = by_name.get((player_name, player_pos))
        if gsis:
            player.nflverse_id = gsis
            name_matched += 1
            matched += 1
            continue

        # Try first word of player name (some have suffixes)
        first_word = player_name.split()[0] if " " in player_name else player_name
        for (n, p), g in by_name.items():
            if p == player_pos and first_word in n:
                player.nflverse_id = g
                name_matched += 1
                matched += 1
                break

        if matched > (name_matched + short_matched):
            continue

        # Try short name
        gsis = by_short.get((player_name, player_pos))
        if gsis:
            player.nflverse_id = gsis
            short_matched += 1
            matched += 1

        if matched % 200 == 0:
            await session.flush()

    await session.commit()

    # New total
    result = await session.execute(
        select(Player).where(Player.nflverse_id.isnot(None))
    )
    total_with_ids = len(result.scalars().all())

    return {
        "matched": matched,
        "name_matches": name_matched,
        "short_matches": short_matched,
        "total_with_ids": total_with_ids,
        "still_missing": len(missing) - matched,
    }
