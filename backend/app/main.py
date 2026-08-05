from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


import logging
import os
from contextlib import asynccontextmanager

# ── Logging config ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from app import task_scheduler

# ── Role-based composition ────────────────────────────────────────
# EARL_ROLE selects which routers this process mounts and whether the
# task scheduler runs. Supports running the same shared codebase on
# multiple dedicated machines:
#
#   "all"     (default, dev box)      → everything + scheduler
#   "api"     (user-facing server)    → v1/mobile, chat, games, results,
#                                       players, teams, stats, articles,
#                                       subscriptions, token_usage. NO scheduler.
#   "compute" (worker server)         → ingest, mlb/nba stats, admin,
#                                       writeups. RUNS the scheduler.
#
# The writeup read endpoints exposed under /api/v1 (mobile) come via
# v1_router on the API box. The compute box exposes the same underlying
# writeups.router for generation + admin.
EARL_ROLE = os.environ.get("EARL_ROLE", "all").strip().lower()
if EARL_ROLE not in ("all", "api", "compute"):
    raise SystemExit(f"Unknown EARL_ROLE={EARL_ROLE!r} (expected all|api|compute)")


def _include(router):
    app.include_router(router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on boot when this role owns task execution, shut down on stop.

    Only "compute" and "all" run the scheduler. The API box is intentionally
    stateless (serves requests only) so tasks never run twice across machines.

    Ignored for non-granian invocations (e.g. tests importing app.main).

    NOTE: Browser is NOT started here. Granian forks worker processes, and
    Playwright can't survive a fork. Instead the browser is created lazily
    on the first scrape request via get_browser() and lives forever in the
    single worker process (--workers 1).
    """
    if EARL_ROLE in ("compute", "all"):
        await task_scheduler.start_scheduler()
    yield
    if EARL_ROLE in ("compute", "all"):
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
    conversations,
    games,
    home,
    ingest,
    mlb_stats,
    nba_stats,
    players,
    props,
    results,
    stats,
    subscriptions,
    teams,
    admin,
    writeups,
    token_usage,
    v1,
)

# Routers grouped by role.
_LEGACY_USER_FACING = [
    auth,
    articles,
    chat,
    chat_nba,
    chat_mlb,
    conversations,
    games,
    home,
    players,
    props,
    results,
    stats,
    subscriptions,
    teams,
    token_usage,
]

_COMPUTE_FACING = [
    ingest,
    mlb_stats,
    nba_stats,
    admin,
    writeups,
]

if EARL_ROLE in ("all", "api"):
    for r in _LEGACY_USER_FACING:
        _include(r.router)
    # v1 re-exposes the mobile-facing subset under /api/v1.
    _include(v1.v1_router)
    _include(token_usage.admin_router)

if EARL_ROLE in ("all", "compute"):
    for r in _COMPUTE_FACING:
        _include(r.router)


@app.get("/")
async def root():
    return {"status": "ok", "version": "1", "role": EARL_ROLE}


@app.get("/health")
async def health():
    return {"status": "healthy", "role": EARL_ROLE}
