"""
API v1 router — the future mobile public API contract.

Mounts the mobile-facing subset of existing routers under /api/v1 so the
mobile contract is stable and decoupled from the internal (/ingest, /admin)
surface, which will keep evolving.

IMPORTANT: This re-uses the EXACT same handlers as the legacy routes. It does
NOT duplicate logic. It only re-exposes the routes under a versioned prefix.

Security: this is the versioned, mobile-friendly surface. Internal routes
(/ingest, /admin) are intentionally NOT included here. When a native app ships,
/ingest and /admin must remain firewalled to internal-only.
"""
from fastapi import APIRouter
from fastapi.routing import APIRoute

from app.routers import (
    auth,
    articles,
    chat,
    chat_nba,
    chat_mlb,
    conversations,
    games,
    home,
    writeups,
    subscriptions,
    token_usage,
)

# Versioned router — everything under /api/v1
v1_router = APIRouter(prefix="/api/v1", tags=["v1"])

# ── Clean-prefix routers (no /api baked into their own prefix) ──────
v1_router.include_router(auth.router)          # /api/v1/auth/...
v1_router.include_router(games.router)         # /api/v1/games, /api/v1/seasons, /api/v1/handicapping/...
v1_router.include_router(home.router)          # /api/v1/home/upcoming-games
v1_router.include_router(chat.router)          # /api/v1/chat
v1_router.include_router(chat_nba.router)      # /api/v1/chat/nba
v1_router.include_router(chat_mlb.router)      # /api/v1/chat/mlb
v1_router.include_router(conversations.router) # /api/v1/chat/conversations/...
v1_router.include_router(writeups.router)      # /api/v1/writeups/... (public + read)

# ── Routers with a baked-in /api prefix ───────────────────────────────
# Their routes are exposed via the clean /api/v1 aliases below (so the mobile
# contract has no double /api/... paths). They are NOT re-mounted here under
# /api/v1/api/... to keep the public surface tidy. Internal-only pieces (e.g.
# subscriptions/webhook, token_usage.admin_router) are intentionally excluded.

# ── Clean aliases (no double /api) for the mobile contract ──────────
# Reuses the exact same endpoints under tidy /api/v1 paths. No logic dup.
_CLEAN_ALIASES = [
    # (source_router, source_path_suffix, clean /api/v1 path)
    (articles.router, "/team/{sport}/{abbreviation}", "/articles/team/{sport}/{abbreviation}"),
    (subscriptions.router, "/plans", "/account/plans"),
    (subscriptions.router, "/my", "/account/my"),
    (subscriptions.router, "/payments", "/account/payments"),
    (subscriptions.router, "/checkout", "/account/checkout"),
    (subscriptions.router, "/cancel", "/account/cancel"),
    (token_usage.router, "/token-usage", "/account/token-usage"),
]


def _install_clean_aliases(target: APIRouter) -> None:
    """Re-register selected routes under tidy /api/v1 paths."""
    for src_router, src_suffix, clean_path in _CLEAN_ALIASES:
        for route in src_router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = getattr(route, "path", "")
            if src_suffix and not path.endswith(src_suffix):
                continue
            # new_path is relative to target's own /api/v1 prefix.
            target.add_api_route(
                clean_path,
                route.endpoint,
                methods=list(route.methods or ["GET"]),
                name=route.name,
                dependencies=list(route.dependencies or []),
                response_model=getattr(route, "response_model", None),
                tags=["v1"],
            )


_install_clean_aliases(v1_router)
