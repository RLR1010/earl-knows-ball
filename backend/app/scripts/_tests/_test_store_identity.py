"""Deterministic test of base_generator.store() identity-lock + slug-alias (no LLM). Throwaway row, cleaned up."""
import asyncio, asyncpg
DSN = "postgresql://earl:goY-4oLs6tGtZlYsX8xx0LSbFbsmX801KSr3O9wcXB2ivmBuPCL12w@localhost:5432/earl_knows_football"
GID = 401874393  # real nfl games.id with no writeup (throwaway: cleaned up)

async def clean(conn):
    await conn.execute("DELETE FROM nfl.game_writeup_slug_aliases a USING nfl.game_writeups w WHERE w.game_id=$1 AND a.game_writeup_id=w.id", GID)
    await conn.execute("DELETE FROM nfl.game_writeups WHERE game_id=$1", GID)

def wdict(game_id, title, slug, sport_id):
    return {
        "game_id": game_id, "title": title, "slug": slug, "keyword": title[:6],
        "content": "PUB", "public_content": "PUB " + title,
        "premium_content": "PREMIUM " + title, "research_brief": {"summary": "RB " + title},
        "quality_checks": [{"check": "ok"}], "status": "published", "version": 1,
        "seo_meta_title": title, "seo_meta_description": "desc", "seo_og_title": title,
        "seo_og_description": "ogd", "social_caption": "cap",
    }

async def go():
    c = await asyncpg.connect(DSN)
    await clean(c)
    import sys
    sys.path.insert(0, "/home/rich/.openclaw/workspace/earl-knows-football/backend")
    from app.database import async_session
    from app.writeups.nfl.generator import NFLWriteupGenerator
    gen = NFLWriteupGenerator()
    async with async_session() as db:
        gen._db = db  # store()/social read self._db (set by routers before generate)
        # 1) first insert (fresh slug)
        wid = await gen.store(GID, wdict(GID, "Alpha Title", "2026-09-08-alpha-title-here", None), [], preserve_identity=False)
        r = await c.fetchrow("SELECT title,slug FROM nfl.game_writeups WHERE game_id=$1", GID)
        print("1) INSERT        ->", dict(r), "id", wid)
        # 2) regen preserve_identity=True; LLM wanted a DIFFERENT title -> must keep Alpha slug/title
        await gen.store(GID, wdict(GID, "COMPLETELY DIFFERENT BETA", "2026-09-08-completely-different-beta", None), [], preserve_identity=True)
        r2 = await c.fetchrow("SELECT title,slug,version,length(public_content) pub,length(premium_content) prem FROM nfl.game_writeups WHERE game_id=$1", GID)
        print("2) preserve=True ->", dict(r2))
        assert r2["title"] == "Alpha Title" and r2["slug"] == "2026-09-08-alpha-title-here", "FAIL identity not preserved"
        # 3) force_new_title (preserve False) with new title -> slug changes + alias recorded
        await gen.store(GID, wdict(GID, "Gamma Final Title", "2026-09-08-gamma-final-title", None), [], preserve_identity=False)
        r3 = await c.fetchrow("SELECT title,slug FROM nfl.game_writeups WHERE game_id=$1", GID)
        al = await c.fetchval(
            "SELECT count(*) FROM nfl.game_writeup_slug_aliases a JOIN nfl.game_writeups w ON w.id=a.game_writeup_id "
            "WHERE w.game_id=$1 AND a.old_slug='2026-09-08-alpha-title-here'", GID)
        print("3) force new     ->", dict(r3), "| alias(alpha->row):", al)
        assert r3["slug"] == "2026-09-08-gamma-final-title", "FAIL new slug"
        assert al == 1, "FAIL alias not recorded"
        id_kept = await c.fetchval("SELECT id FROM nfl.game_writeups WHERE game_id=$1", GID)
        print("    row id stable across insert/regen:", id_kept == wid, id_kept)
        # 4) old slug resolves to canonical row id
        rid = await c.fetchval(
            "SELECT a.game_writeup_id FROM nfl.game_writeup_slug_aliases a WHERE a.old_slug='2026-09-08-alpha-title-here'")
        print("4) old-slug -> row id:", rid, "== canonical:", rid == id_kept)
    await clean(c)
    await c.close()
    print("\nALL IDENTITY-LOCK ASSERTIONS PASSED ✓")

asyncio.run(go())
