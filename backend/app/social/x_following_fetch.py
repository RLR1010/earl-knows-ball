"""Download everyone @earl_knows_ball follows and store them in x_following.

Run on the compute box (prod) from backend/:  ../venv/bin/python -m app.social.x_following_fetch
No post reads happen here (those spend credits); this only pulls the cheap following LIST so a
human can then pick accounts into x_engage_targets (read_posts / want_to_reply).
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# @earl_knows_ball
_UID = "2094907917522628608"

ENG = create_async_engine(os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL"))
Session = async_sessionmaker(ENG)

async def get_token():
    async with Session() as db:
        from app.core.config import settings
        from app.social.x_oauth import get_live_token
        tok = await get_live_token(
            db, (settings.x_client_id or "").strip(), (settings.x_client_secret or "").strip()
        )
    at = tok.get("access_token")
    if not at:
        raise RuntimeError("no stored OAuth2 access token (x_account) for platform 'x'")
    return at

async def fetch_following(at: str) -> list[dict]:
    from xdk import Client
    cli = Client(access_token=at)
    out = []
    try:
        for page in cli.users.get_following(
            _UID, max_results=200,
            user_fields=["username", "name", "description", "verified"],
        ):
            data = getattr(page, "data", None) or []
            for u in data:
                out.append({
                    "x_user_id": str(u.id),
                    "username": getattr(u, "username", None),
                    "name": getattr(u, "name", None),
                    "description": getattr(u, "description", None),
                })
    except Exception as e:
        sys.stderr.write(f"fetch error: {type(e).__name__}: {e}\n")
        sys.stderr.flush()
    return out

async def upsert(rows: list[dict]) -> None:
    async with Session.begin() as db:
        for u in rows:
            # New accounts that we newly follow are added with read_posts = false
            # (tweet collection opt-in). Existing rows keep their read_posts value
            # (we only refresh username/name/description/snapshot on conflict).
            await db.execute(text(
                "INSERT INTO public.x_following"
                " (x_user_id, username, name, description, snapshot_at, read_posts)"
                " VALUES (:id, :un, :nm, :dc, now(), false)"
                " ON CONFLICT (x_user_id) DO UPDATE SET"
                "   username=EXCLUDED.username, name=EXCLUDED.name,"
                "   description=EXCLUDED.description, snapshot_at=now()"),
                {"id": u["x_user_id"], "un": u["username"], "nm": u["name"],
                 "dc": (u["description"] or "")[:4000]})

async def main() -> None:
    at = await get_token()
    print("token loaded; fetching who we follow...")
    rows = await fetch_following(at)
    print(f"fetched {len(rows)}")
    if rows:
        await upsert(rows)
        print("upserted into x_following")

if __name__ == "__main__":
    asyncio.run(main())
