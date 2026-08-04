# Earl Knows Ball — API Route Map & Public-API Readiness

Generated: 2026-08-04

Purpose: Classify every existing API route for a future mobile app -> true public API
(`/api/v1/*`), and to flag which routes are dangerous and must stay internal.

Legend:
- **MOBILE**  = read/data route a mobile app would legitimately call (mobile public API candidate)
- **AUTH**    = auth/session routes required by mobile (token-based)
- **WRITEUP** = content route (mostly read-side for mobile; generation must stay internal/admin)
- **ADMIN**   = internal ops/admin only. NEVER expose publicly.
- **INGEST**  = scheduler/data-pipeline only. NEVER expose publicly (these are the dangerous ones).

---

## AUTH (mobile needs token-based versions)
| Route | Method | Notes |
|-------|--------|-------|
| /auth/send-code | POST | email magic-link/OTP — mobile needs a token variant |
| /auth/verify-code | POST | returns session — must return a bearer token for mobile |
| /auth/logout | POST | |
| /auth/me | GET | current user profile |

## MOBILE (data the app would consume)
| Route | Method | Notes |
|-------|--------|-------|
| /seasons | GET | |
| /games | GET | schedule + consolidated lines (uses betting_lines_consolidated) |
| /games/{game_id} | GET | game detail |
| /games/{game_id}/box-score | GET | |
| /handicapping/predictions/{game_id} | GET | ATS/OU/ML pick card |
| /handicapping/nba/predictions/{game_id} | GET | |
| /handicapping/nfl/prediction-stats/{game_id} | GET | |
| /handicapping/nba/prediction-stats/{game_id} | GET | |
| /home/upcoming-games | GET | home feed |
| /api/articles/team/{sport}/{abbreviation} | GET | team articles |
| /chat | POST | these three chat endpoints are THE core mobile feature |
| /chat/nba | POST | |
| /chat/mlb | POST | |
| /chat/status/{request_id} | GET | polling |
| /chat/conversations/{sport} | GET | history |
| /chat/conversations/{sport}/{conversation_id} | GET | |
| /chat/conversations/{sport}/{conversation_id} | DELETE | |
| /writeups/{sport}/{game_id}/public | GET | **already a public-facing writeup route** |
| /writeups/nfl/{writeup_id} | GET | |
| /writeups/nba/{writeup_id} | GET | |
| /writeups/mlb/{writeup_id} | GET | |
| /writeups/nfl/game/{game_id} | GET | |
| /writeups/nba/game/{game_id} | GET | |
| /writeups/mlb/by-game/{game_id} | GET | |
| /api/users/me/token-usage | GET | account usage |
| /health | GET | could be public liveness |

## WRITEUP (read ok; generation internal)
| Route | Method | Notes |
|-------|--------|-------|
| /writeups/{sport}/{game_id}/preview-public | POST | already has public variant |
| /writeups/generate-public | POST | already has public variant |
| ... internal generate/preview/status | POST/PATCH | **keep internal** — admin/ops triggers |

## ADMIN (NEVER public — full internal surface)
Admin router (`/api/admin/...`): models, training-runs, features, tasks, users, db schemas,
data-loader, prediction-stats, token-limits, articles management. ~92 auth-gated admin routes.

## INGEST (NEVER public — the dangerous surface)
All `/ingest/*` POST routes: scheduling, scrapers, stats refresh, odds, DFS, rosters,
writeup generation triggers, fb-scraper. ~45 routes. These must stay on the private network
and be reachable ONLY from the task box / scheduler, never from the public API.

---

## Versioning status

- **DONE (2026-08-04): `/api/v1` layer implemented and deployed.** `backend/app/routers/v1.py` mounts
  the mobile-facing subset (auth, games, home, chat+conversations, writeups, subscriptions,
  token_usage, articles) under `/api/v1` by re-registering the SAME handlers — no logic duplication.
  Legacy routes untouched and still served. Clean aliases avoid double-`/api` paths
  (`/api/v1/account/...` not `/api/v1/api/...`). Auth dependencies preserved (smoke-tested:
  `/api/v1/auth/me` + `/api/v1/account/token-usage` return 401 unauthenticated; public reads 200).
- Legacy route paths remain NOT versioned (still live for the current web frontend).
- `/ingest` + `/api/admin` are intentionally excluded from `/api/v1` — internal-only.
- 53 clean routes exposed under `/api/v1`; zero double-`/api` paths.

## Security posture for future public API
1. Expose ONLY auth + mobile-read + public-writeup routes under `/api/v1`.
2. Never expose `/ingest/*` or `/api/admin/*` publicly — they stay internal-only
   (firewalled to web/task boxes).
3. Mobile auth = scoped bearer tokens (with refresh), NOT cookies, to avoid
   cross-site cookie/SameSite issues on a mobile client.
4. Add per-user rate limiting on the public `/api/v1` layer (esp. /chat).
5. Keep `/ingest/*` triggering from the scheduler via internal address only — never route
   scheduler traffic through the public Next.js/Caddy layer.
