"""Backfill mlb.venues.timezone with the IANA timezone for each venue.

Venue -> IANA mapping is done by the venue's NAME (case-insensitive) since these
are known, fixed stadiums. US MLB parks -> their local tz; international/special
venues (Tokyo, Seoul, London, Mexico City, Sydney) -> their tz. Used by feature
code that needs the venue-local time (e.g. correct week_number, verifying
day/night from local time).

Usage:
    cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_venues_timezone.py
"""
import asyncio
import asyncpg

DB_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

# Canonical venue -> IANA timezone. MLB name is authoritative.
VENUE_TZ = {
    "American Family Field": "America/Chicago",          # Milwaukee
    "Angel Stadium": "America/Los_Angeles",              # Anaheim
    "BB&T Ballpark": "America/New_York",
    "Ballpark of the Palm Beaches": "America/New_York",  # West Palm Beach FL
    "BayCare Ballpark": "America/New_York",              # Clearwater FL
    "Bristol Motor Speedway": "America/New_York",        # Bristol TN
    "Busch Stadium": "America/Chicago",                  # St. Louis
    "CACTI Park of the Palm Beaches": "America/New_York",
    "Chase Field": "America/Phoenix",                    # Phoenix (no DST)
    "Citi Field": "America/New_York",
    "Citizens Bank Park": "America/New_York",            # Philadelphia
    "Comerica Park": "America/Detroit",
    "CoolToday Park": "America/New_York",                # North Port FL
    "Coors Field": "America/Denver",
    "Daikin Park": "America/Chicago",                    # Houston
    "Dodger Stadium": "America/Los_Angeles",
    "Dunkin' Park": "America/New_York",                  # Hartford
    "Estadio Alfredo Harp Helu": "America/Mexico_City",
    "Estadio de Beisbol": "America/Mexico_City",
    "Estadio de Beisbol Monterrey": "America/Monterrey",
    "Fenway Park": "America/New_York",
    "Field of Dreams": "America/Chicago",                # Dyersville IA
    "Fort Bragg Field": "America/New_York",
    "George M. Steinbrenner Field": "America/New_York",  # Tampa
    "Globe Life Field": "America/Chicago",               # Arlington TX
    "Globe Life Park in Arlington": "America/Chicago",
    "Gocheok Sky Dome": "Asia/Seoul",
    "Great American Ball Park": "America/New_York",      # Cincinnati
    "Hiram Bithorn Stadium": "America/Puerto_Rico",
    "Jungle Jim's Stadium": "America/New_York",
    "Kauffman Stadium": "America/Chicago",               # Kansas City
    "Las Vegas Ballpark": "America/Los_Angeles",
    "London Stadium": "Europe/London",
    "Muncy Bank Ballpark": "America/New_York",           # Williamsport PA
    "Nationals Park": "America/New_York",
    "Oakland Coliseum": "America/Los_Angeles",
    "Oracle Park": "America/Los_Angeles",                # SF
    "Oriole Park at Camden Yards": "America/New_York",
    "PETCO Park": "America/Los_Angeles",                 # San Diego
    "Pinnacle Bank Arena": "America/Chicago",
    "PNC Park": "America/New_York",                      # Pittsburgh
    "Progressive Field": "America/New_York",             # Cleveland
    "Rate Field": "America/Chicago",                     # White Sox, Chicago
    "Rickwood Field": "America/Chicago",                 # Birmingham AL
    "RingCentral Coliseum": "America/Los_Angeles",       # Oakland (alt name)
    "Rogers Centre": "America/Toronto",                  # Toronto
    "Sahlen Field": "America/New_York",                  # Buffalo
    "Sutter Health Park": "America/Los_Angeles",         # West Sacramento, A's
    "Sydney Cricket Ground": "Australia/Sydney",
    "T-Mobile Park": "America/Los_Angeles",              # Seattle
    "TD Ameritrade Park": "America/Chicago",             # Omaha
    "TD Ballpark": "America/Toronto",                    # Dunedin FL (Jays)
    "Target Field": "America/Chicago",                   # Minneapolis
    "Tokyo Dome": "Asia/Tokyo",
    "Tropicana Field": "America/New_York",               # St. Pete
    "Truist Park": "America/New_York",                   # Atlanta
    "Turner Field": "America/New_York",
    "Wrigley Field": "America/Chicago",
    "Yankee Stadium": "America/New_York",
    "loanDepot park": "America/New_York",                # Miami  (case handled)
    # Accent-normalized alias for the Mexico City venue
    "estadio alfredo harp helu": "America/Mexico_City",
}

# Normalize keys by lowercasing/unspacing + stripping accents for tolerant lookup.
import unicodedata


def _norm_key(s: str) -> str:
    s = s.strip().lower()
    # strip combining accents (NFD -> drop marks)
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")

_NORM = {}
for name, tz in VENUE_TZ.items():
    _NORM[_norm_key(name)] = tz


def tz_for(name: str):
    if not name:
        return None
    return _NORM.get(_norm_key(name))


async def main():
    conn = await asyncpg.connect(DB_URL)
    rows = await conn.fetch("SELECT id, name FROM mlb.venues ORDER BY id")
    updated = 0
    missing = []
    for r in rows:
        tz = tz_for(r["name"])
        if tz:
            await conn.execute(
                "UPDATE mlb.venues SET timezone=$1 WHERE id=$2", tz, r["id"]
            )
            updated += 1
        else:
            missing.append((r["id"], r["name"]))
    if missing:
        print("VENUES WITHOUT A MAPPING (need attention):")
        for mid, mname in missing:
            print(f"  id={mid}  {mname}")
    print(f"\nUpdated timezone for {updated}/{len(rows)} venues.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
