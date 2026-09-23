#!/usr/bin/env python3
"""Load active NFL contracts (OverTheCap via nflverse) into nfl.contracts.

Source: nflverse-data ``contracts`` release, ``historical_contracts.parquet``
(the ``.csv.gz`` asset is stale — do NOT use it). Columns of interest:
``apy`` (avg per year, $M) and ``apy_cap_pct`` (share of the salary cap —
position-agnostic, so it is the right quantity to blend as a "market value" factor).

Table: nfl.contracts (gsis_id, player_id, player_name, position, team_abbr,
                      year_signed, years, apy, apy_cap_pct, inflated_apy,
                      guaranteed, is_active)

Usage:
    python -m app.ingestion.load_contracts
"""
from __future__ import annotations

import asyncio
import io
import urllib.request

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

URL = "https://github.com/nflverse/nflverse-data/releases/download/contracts/historical_contracts.parquet"

DDL = """
CREATE TABLE IF NOT EXISTS nfl.contracts (
    gsis_id       varchar(16) NOT NULL,
    player_id     integer,
    player_name   text,
    position      varchar(8),
    team_abbr     varchar(32),
    year_signed   integer NOT NULL,
    years         numeric,
    apy           numeric,
    apy_cap_pct   numeric,
    inflated_apy  numeric,
    guaranteed    numeric,
    is_active     boolean,
    updated_at    timestamptz DEFAULT now(),
    PRIMARY KEY (gsis_id, year_signed)
);
"""


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        f = float(v)
        return None if pd.isna(f) else int(f)
    except (TypeError, ValueError):
        return None


async def main_async() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "earl-knows-ball/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        raw = r.read()
    df = pd.read_parquet(io.BytesIO(raw))
    print(f"parquet rows: {len(df)}")

    # Only the CURRENT contract per player (is_active) drives offseason value.
    act = df[df["is_active"] == True].copy()  # noqa: E712
    act["gsis_id"] = act["gsis_id"].fillna("").astype(str).str.strip()
    act = act[act["gsis_id"] != ""]
    act = act.sort_values("year_signed").drop_duplicates("gsis_id", keep="last")
    print(f"active players: {len(act)}")

    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            await db.execute(text(DDL))
            g2p = {
                r[0]: r[1]
                for r in (
                    await db.execute(
                        text("SELECT nflverse_id, id FROM nfl.players WHERE nflverse_id IS NOT NULL")
                    )
                ).all()
            }
            n = 0
            for _, r in act.iterrows():
                g = r["gsis_id"]
                await db.execute(
                    text(
                        """
                        INSERT INTO nfl.contracts
                            (gsis_id, player_id, player_name, position, team_abbr,
                             year_signed, years, apy, apy_cap_pct, inflated_apy,
                             guaranteed, is_active)
                        VALUES (:g,:pid,:name,:pos,:team,:ys,:yrs,:apy,:cap,:iap,
                                :guar,:act)
                        ON CONFLICT (gsis_id, year_signed) DO UPDATE SET
                            player_id=EXCLUDED.player_id, player_name=EXCLUDED.player_name,
                            position=EXCLUDED.position, team_abbr=EXCLUDED.team_abbr,
                            years=EXCLUDED.years, apy=EXCLUDED.apy,
                            apy_cap_pct=EXCLUDED.apy_cap_pct,
                            inflated_apy=EXCLUDED.inflated_apy,
                            guaranteed=EXCLUDED.guaranteed, is_active=EXCLUDED.is_active,
                            updated_at=now()
                        """
                    ),
                    {
                        "g": g,
                        "pid": g2p.get(g),
                        "name": r.get("player"),
                        "pos": (r.get("position") or None),
                        "team": (r.get("team") or None),
                        "ys": _int(r.get("year_signed")),
                        "yrs": _num(r.get("years")),
                        "apy": _num(r.get("apy")),
                        "cap": _num(r.get("apy_cap_pct")),
                        "iap": _num(r.get("inflated_apy")),
                        "guar": _num(r.get("guaranteed")),
                        "act": True,
                    },
                )
                n += 1
            await db.commit()
            matched = await db.scalar(
                text("SELECT count(*) FROM nfl.contracts WHERE player_id IS NOT NULL")
            )
            print(f"upserted {n}; matched to players: {matched}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main_async())
