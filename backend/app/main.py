from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


import logging
from contextlib import asynccontextmanager

# ── Logging config ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from app import task_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on boot, shut down on stop."""
    await task_scheduler.start_scheduler()
    yield
    await task_scheduler.stop_scheduler()


app = FastAPI(lifespan=lifespan,
    title="Earl Knows Ball",
    version="1.0.0",
    
)

# ── CORS (allow frontend from any origin) ────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── All Routes ─────────────────────────────────────────────────────

from app.routers import (
    auth,
    articles,
    chat,
    chat_nba,
    chat_mlb,
    games,
    ingest,
    mlb_stats,
    nba_stats,
    players,
    stats,
    subscriptions,
    teams,
    admin,
    writeups,
)

app.include_router(auth.router)
app.include_router(articles.router)
app.include_router(chat.router)
app.include_router(chat_nba.router)
app.include_router(chat_mlb.router)
app.include_router(games.router)
app.include_router(ingest.router)
app.include_router(mlb_stats.router)
app.include_router(nba_stats.router)
app.include_router(players.router)
app.include_router(stats.router)
app.include_router(subscriptions.router)
app.include_router(teams.router)
app.include_router(admin.router)
app.include_router(writeups.router)


@app.get("/")
async def root():
    return {"status": "ok", "version": "1"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
