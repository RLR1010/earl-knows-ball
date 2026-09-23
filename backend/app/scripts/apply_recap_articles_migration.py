"""Idempotent migration: add post-game recap support to ``original_articles``.

Post-game recaps are stored as ``original_articles`` rows with
``section='recap'`` so they automatically surface in the sport + team Article
listings (and get their own ``/<sport>/articles/<slug>`` SEO page).

Adds:
  * ``game_id`` (bigint, nullable) — links a recap to the game it covers.
  * a partial unique index ``(sport, game_id) WHERE section='recap'`` so a game
    can only ever have one recap (idempotent generation).

Safe to run repeatedly. Uses the admin engine for DDL.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.database import admin_async_session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("earl.recaps.migration")


STATEMENTS = [
    "ALTER TABLE public.original_articles ADD COLUMN IF NOT EXISTS game_id bigint",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_original_articles_recap_game "
        "ON public.original_articles (sport, game_id) "
        "WHERE section = 'recap' AND game_id IS NOT NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_original_articles_recap_pub "
        "ON public.original_articles (sport, published_at DESC) "
        "WHERE section = 'recap'"
    ),
]


async def main() -> None:
    async with admin_async_session() as session:
        for stmt in STATEMENTS:
            await session.execute(text(stmt))
            log.info("ok: %s", stmt)
        await session.commit()
    # Report final shape.
    async with admin_async_session() as session:
        row = await session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='original_articles' AND column_name='game_id'"
            )
        )
        for r in row:
            log.info("column: %s %s", r[0], r[1])
    log.info("recap migration complete")


if __name__ == "__main__":
    asyncio.run(main())
