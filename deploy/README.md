# Deploy / Blue‑Green Frontend

Zero‑downtime frontend deploys for earlknowsball.com. This replaces the old
"rebuild `.next` in place while `next start` serves from it" flow, which could
serve half‑built assets and drop requests.

## Architecture (frontend host 70.231.2.226)

Caddy (edge/TLS) load‑balances across TWO Next.js instances, each with its own
build directory and its own port:

| Instance | Port | systemd unit                 | working dir                                     | EARL_INSTANCE |
|----------|------|------------------------------|-------------------------------------------------|---------------|
| GREEN    | 3000 | `earl-frontend.service`      | `/home/rich/earl-knows-football/frontend`       | `green` (via drop‑in) |
| BLUE     | 3001 | `earl-frontend-blue.service` | `/home/rich/earl-knows-football/frontend-blue`  | `blue`        |

Caddy (`deploy/Caddyfile` → the `frontend_lb` snippet) probes each instance at
`/healthz` (a Next route added at `frontend/src/app/healthz/route.ts`, JSON, no
backend dependency). An unhealthy instance is dropped from round‑robin.

`next start` and `next build` share NO code state across instances: each reads
its own `.next`.

## Golden rule

**Never run `npm run build` in a live instance's dir.** Always deploy through
`deploy-frontend.sh`, which:
1. health‑checks the *sibling* (the instance that will carry traffic),
2. **drains the target from Caddy** (graceful reload routing only to the peer),
3. stops the target, builds it, health‑checks it,
4. reloads Caddy to include it again, then verifies `/mlb`.

Two Caddy reloads per target; each reload is graceful/zero‑downtime and the
target never receives a request while `.next` is being rewritten.

## Deploy (on the frontend box, from the `rich` account)

```bash
# sync your changed source onto the box FIRST (scp/rsync into BOTH dirs — see
# "Syncing source" below), then:
sudo /usr/local/bin/deploy-frontend          # rebuild BOTH (blue then green)
sudo /usr/local/bin/deploy-frontend green    # rebuild only GREEN  (blue serves)
sudo /usr/local/bin/deploy-frontend blue     # rebuild only BLUE   (green serves)
```

The script is version‑controlled at `backend/scripts/deploy-frontend.sh` and
installed on the box at `/usr/local/bin/deploy-frontend`. After changing it in
git, re‑scp it over so the box runs the committed version.

## Syncing source to the box

The frontend host's source is **not a git checkout**; it is synced from this
repo (or the dev machine) by scp/rsync. Two important facts:

- Without changes this is a per‑file copy. To receive a change, that file must
  be copied onto the box.
- Edit files so their content lands in BOTH `frontend/` and `frontend-blue/`.
  Prefer **hardlink sync** (`cp -al frontend/<f> frontend-blue/<f>`) so the two
  dirs share inodes and never drift — see the clone recipe below.

A deploy therefore = sync changed files to the box (both dirs) + run
`deploy-frontend`. The box is picked up in whichever copy is rebuilt next.

## How `frontend-blue/` was created (and how to clone *new* files to it)

The two dirs share source via hardlinks (identical inodes → identical content,
no drift, ~0 extra disk). `node_modules` and `.next` are independent:

- `node_modules` is a **hardlink copy** (`cp -al node_modules frontend-blue/`),
  NOT a symlink — Next 16/Turbopack rejects a symlinked node_modules
  ("points out of the filesystem root").
- `.next` is a real, independent dir created by each build.

```bash
SRC=/home/rich/earl-knows-football/frontend
DST=/home/rich/earl-knows-football/frontend-blue
# Already done; only do this again if you want a clean rebuild of the pair.
for f in Dockerfile .dockerignore earl-frontend.service .env.local next.config.js \
         next-env.d.ts package.json package-lock.json postcss.config.js \
         tailwind.config.js tsconfig.json public src; do
  cp -al "$SRC/$f" "$DST/" 2>/dev/null || cp -a "$SRC/$f" "$DST/"
done
cp -al "$SRC/node_modules" "$DST/node_modules"
```

> ⚠️ A hardlink clone is a snapshot of the entries present at clone time. If you
> add a **new** file/dir to `frontend/` after the clone, hardlink it into
> `frontend-blue/` too (or re‑run the loop). Edited existing files are already
> shared and need nothing.

## Health route

`/healthz` → `frontend/src/app/healthz/route.ts` returns
`{"status":"ok","role":"frontend","instance":"green"|"blue",...}` purely from
the Next process (no API/DB dependency), so Caddy knows each instance booted
independently. It must stay dependency‑free.

## Reference files in this dir

- `Caddyfile` — live config at `/etc/caddy/Caddyfile` (post‑fmt). comic.com host
  was removed (unrelated legacy site no longer served).
- `earl-frontend.service` — GREEN unit (live copy).
- `earl-frontend-blue.service` — BLUE unit (live copy).
- `earl-frontend.service.d/instance.conf` — drop‑in setting `EARL_INSTANCE=green`.

## Notes

- Frontend services are **system** units on prod (`sudo systemctl ...`), not
  the `systemctl --user` user units used on the dev machine.
- `.env.local` with `NEXT_PUBLIC_*` is inlined at **build** time, so a build
  must run in a dir that has the correct `.env.local` (both hardlinked).
- API (8001/192.168.1.146) and compute (8002/192.168.1.140) are separate boxes
  and unaffected by frontend rebuilds.
