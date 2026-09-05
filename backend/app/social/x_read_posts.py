"""X post reader @earl_knows_ball - the 2 most RECENT original posts per followed account.

Goal (Rich 2026-09-02): grab the 2 most recent posts from each account we follow, to surface
current takes relevant to the games we are handicapping NOW. Not a deep-dive; not year-old
content. Every stored post carries the tweet's own created_at timestamp.

Cost control (Rich credit-conscious; over-spent once on a wrong deep-dive, ~$1.36):
- Default = SMALL TEST of 3 accounts so Rich can approve output before a full sweep.
- Exclude replies+retweets server-side (original content only).
- Staleness guard: posts older than MAX_AGE_DAYS are NOT persisted (no ancient/irrelevant takes).
- Exact dedup: UNIQUE(tweet_id) ON CONFLICT DO NOTHING - never stores/re-bills dupes.

Run on compute (prod):
    ../venv/bin/python -m app.social.x_read_posts             # 3-account test (default)
    ../venv/bin/python -m app.social.x_read_posts --accounts 59
    ../venv/bin/python -m app.social.x_read_posts --accounts 5
"""
import argparse, asyncio, os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

POSTS_PER_ACCOUNT = 5      # Rich: pull the 5 most recent original posts per picked account
MAX_AGE_DAYS = 14          # only persist posts younger than this (ignore year-old / ancient takes)
DEFAULT_TEST_ACCOUNTS = 3  # safe default: tiny test before a full sweep

ENG = create_async_engine(os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL"))
Session = async_sessionmaker(ENG)


def _parse_ts(v):
    if not v or not isinstance(v, str):
        return v
    try:
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


async def get_token():
    async with Session() as db:
        from app.core.config import settings
        from app.social.x_oauth import get_live_token
        tok = await get_live_token(
            db, (settings.x_client_id or "").strip(), (settings.x_client_secret or "").strip()
        )
    at = tok.get("access_token")
    if not at:
        raise RuntimeError("no stored OAuth2 access token")
    return at


async def targets(usernames=None):
    async with Session() as db:
        sql = ("SELECT x_user_id, username, name FROM public.x_following "
               "WHERE read_posts = TRUE")
        params = {}
        if usernames:
            sql += " AND username = ANY(:us)"
            params["us"] = usernames
        sql += " ORDER BY name"
        rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def max_stored_tweet(author_id):
    """Highest tweet id we already hold for this author (dedup)."""
    async with Session() as db:
        return (await db.execute(text(
            "SELECT tweet_id FROM public.x_posts WHERE author_user_id=:a "
            "ORDER BY tweet_id DESC LIMIT 1"), {"a": author_id})).scalar()


def fetch_recent(at, acc, since_id):
    """Return up to POSTS_PER_ACCOUNT newest ORIGINAL posts for acc (each w/ parsed created_at)."""
    from xdk import Client
    cli = Client(access_token=at)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAX_AGE_DAYS)
    out = []
    try:
        page = next(cli.users.get_posts(
            acc["x_user_id"], max_results=POSTS_PER_ACCOUNT,
            exclude=["replies", "retweets"],
            since_id=since_id,               # only ask for posts newer than we already hold
            post_fields=["created_at", "public_metrics"],
        ), None)
        data = getattr(page, "data", None) or []
        for pp in data[:POSTS_PER_ACCOUNT]:
            created = _parse_ts(getattr(pp, "created_at", None))
            if created is not None and created < cutoff:
                continue                      # ancient - do not persist
            pm = getattr(pp, "public_metrics", None)
            out.append({
                "tweet_id": str(pp.id),
                "author_user_id": acc["x_user_id"],
                "author_username": acc["username"],
                "text": getattr(pp, "text", ""),
                "created_at": created,
                "likes": getattr(pm, "like_count", None) if pm else None,
                "retweets": getattr(pm, "retweet_count", None) if pm else None,
                "replies": getattr(pm, "reply_count", None) if pm else None,
            })
    except Exception as e:
        sys.stderr.write(f"  @{acc['username']}: {type(e).__name__}: {str(e)[:200]}\n")
        sys.stderr.flush()
    return out


async def store_many(posts):
    async with Session.begin() as db:
        for p in posts:
            await db.execute(text(
                "INSERT INTO public.x_posts (tweet_id, author_user_id, author_username, text,"
                " created_at, read_at, likes, retweets, replies)"
                " VALUES (:tid,:aid,:au,:tx,:cr,now(),:lk,:rt,:rp)"
                " ON CONFLICT (tweet_id) DO NOTHING"),
                {"tid": p["tweet_id"], "aid": p["author_user_id"], "au": p["author_username"],
                 "tx": p["text"], "cr": p["created_at"], "lk": p.get("likes"),
                 "rt": p.get("retweets"), "rp": p.get("replies")})


async def main(account_cap, usernames=None):
    at = await get_token()
    tgt = await targets(usernames=usernames)
    if usernames:
        tgt = tgt
    else:
        tgt = tgt[:account_cap]
    print(f"read {len(tgt)} account(s), {POSTS_PER_ACCOUNT} newest original post(s) each "
          f"(persist only < {MAX_AGE_DAYS}d old)")
    stored_total = 0
    for i, acc in enumerate(tgt):
        since = await max_stored_tweet(acc["x_user_id"])
        posts = fetch_recent(at, acc, since)
        if posts:
            await store_many(posts)
            stored_total += len(posts)
            for p in posts:
                age_h = None
                if p["created_at"] is not None:
                    age_h = (dt.datetime.now(dt.timezone.utc) - p["created_at"]).total_seconds() / 3600
                created_s = p["created_at"].isoformat() if p["created_at"] else None
                print(f"  @{acc['username']} stored (created {created_s}, "
                      f"age {age_h:.1f}h) :: {p['text'][:60]!r}")
        else:
            print(f"  @{acc['username']}: none recent or reached (may already have stored it)")
    print(f"DONE: {stored_total} recent posts stored across {len(tgt)} account(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=DEFAULT_TEST_ACCOUNTS,
                    help=f"max accounts to read (default {DEFAULT_TEST_ACCOUNTS}); ignored if --users set")
    ap.add_argument("--users", type=str, default=None,
                    help="comma-separated usernames to read (e.g. AdamSchefter,wojespn,RapSheet)")
    args = ap.parse_args()
    users = None
    if args.users:
        users = [u.strip() for u in args.users.split(",") if u.strip()]
    asyncio.run(main(args.accounts, usernames=users))
