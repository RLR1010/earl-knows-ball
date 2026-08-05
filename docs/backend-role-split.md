# Backend Role Split — 4-Machine Migration

## The split (one shared codebase, two entrypoints)

The backend is ONE Python package (`backend/app/`). The same code builds three
different runtime roles by reading `EARL_ROLE` in `app/main.py`:

| EARL_ROLE | Mounts | Scheduler | Runs on |
|-----------|--------|-----------|---------|
| `all` (default) | everything | YES | dev box (today) |
| `api` | v1/mobile (56), chat (9), games, results, players, teams, stats, articles, subscriptions, token | **NO** | user-facing API server |
| `compute` | /ingest (19), writeups (27), mlb/nba stats, admin dashboard (40) | YES | worker server |

**Key rule:** only `compute` and `all` start the task scheduler. The `api` box is
stateless (serves requests only), so tasks never run twice across machines. No
advisory locking needed at this stage because exactly one box runs tasks.

## Entrypoints / run scripts (`backend/`)

- `run_api.sh`      — honors `EARL_ROLE`, defaults to `all` (dev box unchanged)
- `run_all.sh`      — forces `EARL_ROLE=all`
- `run_compute.sh`  — forces `EARL_ROLE=compute`

Each `exec`s Granian `app.main:app` on `0.0.0.0:8001`.

## Proposed 4-machine layout

```
Machine             EARL_ROLE   Runs                                Notes
─────────────────────────────────────────────────────────────────────────
1. db               (none)      PostgreSQL                          never in compose
2. frontend+caddy   (none)      Next.js `npm run start` + Caddy      only public box (443)
3. api              api         granian v1/chat/games + chat embeddings (Ollama GPU)
4. compute          compute     scheduler + scrapers + writeups + predictions + writeup embeddings (Ollama GPU)
```

The `EARL_ROLE` code split already exists. The remaining migration work is
outside the code split (see below).

## Already handled in this refactor

- `app/main.py` mounts routers by role and gates scheduler start on `compute|all`.
- `run_api.sh` / `run_compute.sh` / `run_all.sh` launchers.
- API box exposes **zero** `/ingest` routes and **zero** operational `/api/admin`
  dashboard routes; compute does not expose `v1`/`chat`.

## Open decisions / next steps (not yet implemented)

1. **Writeups are split across roles by design** — the mobile `/api/v1/writeups`
   read endpoints live on `api` (via `v1_router`); generate/admin writeup routes
   live on `compute`. They hit the same DB, so reads from `v1` see writes from
   `compute`. No change needed, just confirm this is intended.

2. **DB reachability** — `DB_HOST` must point at the db machine from api +
   compute. Currently defaults to localhost. All DB access is env-driven already.

3. **Model store** — `data/models/` (live), ~25k files. api + compute boxes both
   need it for predictions. Define a source-of-truth host + one-way sync / git
   LFS / object storage.

4. **Embedding split** — chat embeddings on api box's Ollama; writeups/articles
   embeddings on compute box's Ollama. `ollama_embed.py` already supports
   multiple hosts; param them per role.

5. **FD scraper** — keep on compute only (DataDome ~1-2 sessions/IP/day); do not
   run from anywhere else.

6. **systemd units** — dev: `earl-backend.service` (defaults `all`). Prod:
   `earl-api.service` + `earl-compute.service` with `EARL_ROLE` set / using the
   role launchers.

7. **Backups** — off-machine (db box dumps to a second location).

## Test on dev box

Run a second granian in `compute` role to prove the split (port 8002) while the
dev service keeps running `all`:
```bash
cd backend && EARL_ROLE=compute ./run_api.sh  # change port to 8002 first, or stop dev service
```
Better: temporarily stop `earl-backend.service`, then start compute alone.
