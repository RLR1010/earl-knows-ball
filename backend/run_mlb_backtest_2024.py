"""Run MLB 2024 backtest, save results to DB and JSON."""
import asyncio, json, os, sys, logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

sys.path.insert(0, "/app")
os.environ["PYTHONPATH"] = "/app:" + os.environ.get("PYTHONPATH", "")

from app.database import async_session, engine
from sqlalchemy.ext.asyncio import AsyncSession
from app.handicapping.mlb.mlb_engine import backtest_season


async def main():
    print("=== MLB 2024 Backtest ===")

    async with AsyncSession(engine) as db:
        results = await backtest_season(db, year=2024, resume=False)

    print(f"\n=== DONE ===")
    print(f"Results: {json.dumps(results, indent=2)}")

    out_path = "/app/app/models/mlb_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump({"year": 2024, "results": results}, f, indent=2)
    print(f"Saved results to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
