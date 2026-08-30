#!/usr/bin/env python3
"""Ensure every MLB + NBA + NFL team has an active WEEKLY team-scoped
auto-generation config, with generation days anchored to weekdays.

Supports per-sport day cycles:
  - MLB/NBA  : Monday, Tuesday, Wednesday, Thursday
  - NFL      : Tuesday, Wednesday  (user wants only Tue/Wed)

Template (matches the Angels setup):
  - cadence: weekly, scope_type: team, section: article
  - reasoning: minimal, visibility: public, word_min: 400, word_max: 700
  - title_mode: llm  (LLM chooses title; `title` is the fixed/seed title)
  - instructions: Angels-style "most compelling story of the week" prompt, no betting advice
  - title: "Weekly {TeamName} News"

Day anchoring (the runner's `_is_due_time_of_day` weekly path):
  - A weekly config is "due" only on the SAME weekday as its last_generated_at.
  - So we set generate_time = "08:00" and last_generated_at = the most recent
    occurrence of the assigned weekday at 07:00 CT (before the 08:00 target).
  - Result: runs at/after 08:00 on the assigned weekday only.

Idempotent: safe to re-run. Also fixes known data issues:
  - NBA id14: team_id=5 (Chicago Bulls) had abbr=CHA (wrong) -> CHI.
  - NBA DAL duplicate (id19 + id23) -> deactivate id23, keep id19.
"""
import asyncio
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.database import async_session

CT = ZoneInfo("America/Chicago")

# Per-sport id ranges and weekday cycles. Weekday ints match datetime.weekday().
SPORTS = ["mlb", "nba", "nfl"]
DAY_CYCLES = {
    "mlb": [0, 1, 2, 3],   # Mon Tue Wed Thu
    "nba": [0, 1, 2, 3],   # Mon Tue Wed Thu
    "nfl": [1, 2],          # Tue Wed (user: articles just on Tue and Wed)
}
ID_RANGES = {
    "mlb": (1, 30),
    "nba": (1, 30),
    "nfl": (1, 32),
}

# Load actual name+abbr from the teams tables so nothing is hardcoded wrong.
async def load_teams(db, sport: str, lo: int, hi: int) -> dict[int, dict]:
    r = await db.execute(
        text(f"SELECT id, name, abbreviation FROM {sport}.teams WHERE id BETWEEN {lo} AND {hi}")
    )
    return {row["id"]: {"name": row["name"], "abbr": row["abbreviation"]} for row in r.mappings()}

def anchor_dt(weekday: int):
    """Yesterday's 07:00 CT if weekday==today, else the most recent 'weekday' at 07:00 CT.
    Guarantees the anchor weekday == `weekday` and time < 08:00 target."""
    today = datetime.now(CT).date()
    delta = (today.weekday() - weekday) % 7
    target_day = today - timedelta(days=delta)
    return datetime.combine(target_day, time(7, 0), tzinfo=CT)

DRY = "--dry-run" in sys.argv

async def main():
    async with async_session() as db:
        # Load canonical teams + existing weekly team configs
        teams_by_sport = {}
        for sport in SPORTS:
            lo, hi = ID_RANGES[sport]
            teams_by_sport[sport] = await load_teams(db, sport, lo, hi)
        r = await db.execute(text(
            "SELECT id, sport, team_id, team_abbr, status FROM public.auto_generation_configs "
            "WHERE cadence='weekly' AND scope_type='team' AND team_id IS NOT NULL"
        ))
        existing = {}
        for row in r.mappings():
            existing.setdefault((row["sport"], row["team_id"]), []).append(dict(row))

        DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday"}
        created, updated, skipped = [], [], []

        # ---- Fix NBA id14: Chicago Bulls team_id=5 had wrong abbr (CHA) ----
        fixed_abbr = await db.execute(text(
            "SELECT id FROM public.auto_generation_configs "
            "WHERE sport='nba' AND team_id=5 AND team_abbr='CHA'"
        ))
        if fixed_abbr.rowcount:
            await db.execute(text(
                "UPDATE public.auto_generation_configs SET team_abbr='CHI' "
                "WHERE sport='nba' AND team_id=5 AND team_abbr='CHA'"
            ))
            print("FIXED: NBA Bulls abbr CHA -> CHI")

        # ---- Deactivate NBA DAL duplicate: keep lower id, deactivate higher ----
        dal = sorted([c["id"] for c in existing.get(("nba", 6), [])])
        # NOTE: existing is keyed by team_id; DAL=6 handled by team_id, not abbr
        if len(dal) > 1:
            for dup_id in dal[1:]:
                await db.execute(text(
                    "UPDATE public.auto_generation_configs SET status='inactive' WHERE id=:i"
                ), {"i": dup_id})
                print(f"DEDUPE: deactivated NBA DAL config id={dup_id} (keeping id={dal[0]})")

        for sport in SPORTS:
            teams = teams_by_sport[sport]
            cycle = DAY_CYCLES[sport]
            lo, hi = ID_RANGES[sport]
            for team_id in range(lo, hi + 1):
                name = teams[team_id]["name"]
                abbr = teams[team_id]["abbr"]
                # Strip common prefixes for a clean team name for the title/instructions.
                short = name.replace("Los Angeles ", "").replace("New York ", "") \
                            .replace("New Orleans ", "").replace("San Francisco ", "") \
                            .replace("San Diego ", "").replace("St. Louis ", "") \
                            .replace("Tampa Bay ", "").replace("Kansas City ", "") \
                            .replace("Golden State ", "")
                weekday = cycle[(team_id - lo) % len(cycle)]
                day_name = DAY_NAMES[weekday]
                gen_time = "08:00"
                anchor = anchor_dt(weekday)

                instructions = (
                    f"Look at the news for the {name} over the last week and decide on "
                    f"what is the most compelling story of the week to write about. "
                    f"Incorporate how the news might affect the team's performance for "
                    f"the upcoming/or current season.  This is a public article so do "
                    f"not give any betting advice."
                )
                title = f"Weekly {short} News"
                fields = dict(
                    sport=sport,
                    title=title,
                    cadence="weekly", generate_time=gen_time,
                    scope_type="team", team_id=team_id, team_abbr=abbr, team_name=name,
                    section="article", status="active",
                    reasoning="minimal", visibility="public",
                    word_min=400, word_max=700, title_mode="llm",
                    instructions=instructions,
                )

                rows = existing.get((sport, team_id), [])
                active_rows = [c for c in rows if c["status"] == "active"]

                if rows:
                    # Update the first active config (or first row) with correct settings.
                    target = active_rows[0] if active_rows else rows[0]
                    set_cols = [k for k in fields if k not in ("title",)]
                    assignments = ", ".join(f"{k}=:{k}" for k in set_cols)
                    params = {k: fields[k] for k in set_cols}
                    params["cid"] = target["id"]
                    await db.execute(text(
                        f"UPDATE public.auto_generation_configs SET {assignments} "
                        f"WHERE id=:cid"
                    ), params)
                    # Anchor last_generated_at to the assigned weekday (07:00 CT).
                    await db.execute(text(
                        "UPDATE public.auto_generation_configs SET "
                        "last_generated_at=:a WHERE id=:cid"
                    ), {"a": anchor, "cid": target["id"]})
                    updated.append(f"{sport.upper()} {abbr} -> {day_name} {gen_time} (id={target['id']})")
                else:
                    cols = list(fields.keys()) + ["last_generated_at"]
                    placeholders = ", ".join(f":{k}" for k in cols)
                    params = {**fields, "last_generated_at": anchor}
                    await db.execute(text(
                        f"INSERT INTO public.auto_generation_configs ({', '.join(cols)}) "
                        f"VALUES ({placeholders})"
                    ), params)
                    created.append(f"{sport.upper()} {abbr} -> {day_name} {gen_time}")

        if not DRY:
            await db.commit()
        else:
            await db.rollback()

    print("\n[DRY-RUN — nothing committed]" if DRY else "\n[APPLIED — committed]")
    print("\n=== CREATED ===")
    for c in created: print("  ", c)
    print("\n=== UPDATED/ANCHORED ===")
    for u in updated: print("  ", u)
    print(f"\nTotal created: {len(created)} | Total updated/anchored: {len(updated)}")

asyncio.run(main())
