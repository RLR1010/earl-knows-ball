#!/usr/bin/env python3
"""
Fix SBR moneylines in-place.

The SBR (SportsbookReview) dataset has a known issue where both home/away
moneylines sometimes have the same sign (both positive or both negative),
which is mathematically impossible. This typically means the sign on one
moneyline is wrong.

Fix rule: When both moneylines have the same sign, flip the sign on the
one with the smaller absolute value. The rationale is that smaller absolute
values (~+/-100 to +/-120) are most likely "near-pick'em" lines that
accidentally lost or gained a negative sign, while larger absolute values
(~-150 to -300) are clearly the favorite and are more likely correct.

This applies independently to:
  - Closing moneylines (home_moneyline, away_moneylines)
  - Opening moneylines (opening_home_moneyline, opening_away_moneyline)

After fixing closing moneylines, we also recalculate implied probabilities.
"""
import asyncio
import logging
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_sbr")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football",
)


def _implied_probability(american_odds: int) -> float | None:
    """Convert American moneyline odds to implied probability (0-1)."""
    if american_odds is None or american_odds == 0:
        return None
    if american_odds > 0:
        return round(100 / (american_odds + 100), 4)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100), 4)


def fix_ml_pair(home_ml: int | None, away_ml: int | None) -> tuple[int | None, int | None]:
    """
    Fix a pair of moneylines that are incorrectly both the same sign.
    Returns (fixed_home_ml, fixed_away_ml).
    
    Rule: If both are the same sign (both positive or both negative),
    flip the sign on the one with the smaller absolute value.
    """
    if home_ml is None or away_ml is None:
        return home_ml, away_ml
    
    # Both positive
    if home_ml > 0 and away_ml > 0:
        if abs(home_ml) <= abs(away_ml):
            return -home_ml, away_ml  # flip home
        else:
            return home_ml, -away_ml  # flip away
    
    # Both negative
    if home_ml < 0 and away_ml < 0:
        if abs(home_ml) <= abs(away_ml):
            return abs(home_ml), away_ml  # flip home to positive
        else:
            return home_ml, abs(away_ml)  # flip away to positive
    
    # Already correct (opposite signs)
    return home_ml, away_ml


async def fix_sbr_moneylines():
    """Fix all SBR moneylines in-place."""
    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with Session() as db:
        # Fetch all SBR lines
        r = await db.execute(
            text("""
                SELECT id, home_moneyline, away_moneyline,
                       opening_home_moneyline, opening_away_moneyline,
                       home_implied_probability, away_implied_probability
                FROM mlb.betting_lines
                WHERE source = 'sbr_historical'
                ORDER BY id
            """)
        )
        rows = r.fetchall()
        total = len(rows)
        logger.info(f"Loaded {total} SBR lines for fixing")
        
        fix_stats = {
            "total": total,
            "close_fixed": 0,
            "open_fixed": 0,
            "implied_recalc": 0,
            "errors": 0,
        }
        
        batch = []
        batch_size = 500
        
        for row in rows:
            try:
                line_id = row[0]
                home_ml = row[1]      # closing home
                away_ml = row[2]      # closing away
                open_home = row[3]    # opening home
                open_away = row[4]    # opening away
                old_home_prob = row[5]
                old_away_prob = row[6]
                
                updates = {}
                
                # Fix closing moneylines
                fixed_home, fixed_away = fix_ml_pair(home_ml, away_ml)
                if (fixed_home != home_ml) or (fixed_away != away_ml):
                    updates["home_moneyline"] = fixed_home
                    updates["away_moneyline"] = fixed_away
                    fix_stats["close_fixed"] += 1
                
                # Fix opening moneylines  
                fixed_open_home, fixed_open_away = fix_ml_pair(open_home, open_away)
                if (fixed_open_home != open_home) or (fixed_open_away != open_away):
                    updates["opening_home_moneyline"] = fixed_open_home
                    updates["opening_away_moneyline"] = fixed_open_away
                    fix_stats["open_fixed"] += 1
                
                # Recalculate implied probabilities from fixed closing moneylines
                new_home_prob = _implied_probability(fixed_home)
                new_away_prob = _implied_probability(fixed_away)
                if new_home_prob != old_home_prob or new_away_prob != old_away_prob:
                    updates["home_implied_probability"] = new_home_prob
                    updates["away_implied_probability"] = new_away_prob
                    fix_stats["implied_recalc"] += 1
                
                if updates:
                    sets = ", ".join(f"{k} = :{k}" for k in updates)
                    params = {"id": line_id, **updates}
                    batch.append(params)
                    
                    if len(batch) >= batch_size:
                        await db.execute(
                            text(f"UPDATE mlb.betting_lines SET {sets} WHERE id = :id"),
                            batch,
                        )
                        await db.flush()
                        logger.info(f"  Fixed {fix_stats['close_fixed']} close + {fix_stats['open_fixed']} open so far...")
                        batch = []
            
            except Exception as e:
                logger.error(f"Error fixing line {row[0]}: {e}")
                fix_stats["errors"] += 1
        
        if batch:
            # Build multi-update for remaining batch
            for params in batch:
                sets = ", ".join(f"{k} = :{k}" for k in params if k != "id")
                await db.execute(
                    text(f"UPDATE mlb.betting_lines SET {sets} WHERE id = :id"),
                    params,
                )
            await db.flush()
        
        await db.commit()
        logger.info(
            f"\n{'='*60}\n"
            f"Fix complete!\n"
            f"  Total lines checked: {fix_stats['total']}\n"
            f"  Closing moneylines fixed: {fix_stats['close_fixed']}\n"
            f"  Opening moneylines fixed: {fix_stats['open_fixed']}\n"
            f"  Implied probs recalculated: {fix_stats['implied_recalc']}\n"
            f"  Errors: {fix_stats['errors']}\n"
            f"{'='*60}"
        )
    
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(fix_sbr_moneylines())
