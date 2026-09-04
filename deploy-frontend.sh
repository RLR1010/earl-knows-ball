#!/usr/bin/env bash
#
# deploy-frontend.sh — zero-downtime Next.js deploy for Earl Knows Ball (blue/green).
# BUILD-ONCE ~> RUN THE SAME BUILD ON BOTH.  (Option A, Rich-approved 2026-09-02 16:53)
#
# WHY (the bug this fixes): the OLD script ran `next build` SEPARATELY inside each instance
# dir. Next 16 (Turbopack) is NON-DETERMINISTIC: identical source produced DIFFERENT hashed
# asset filenames + DIFFERENT BUILD_IDs per instance (green had 0vdscogesgpqp.css, blue had
# 3j6rzy8kxn2o_.css). Caddy round-robins each request, and hashed asset URLs only exist in one
# build — so a page's HTML could load from green (referencing green's css) while the browser's
# css fetch round-robins to blue (missing that file) => intermittent broken CSS on every deploy.
# FIX: build the site ONCE into an isolated scratch clone, then deploy that SAME byte-identical
# .next to BOTH instances. Rotation can never split a page from its assets again.
#
# Zero downtime is preserved exactly as before: each instance is fully drained out of Caddy
# (sed the single reverse_proxy upstream line to peer-only -> caddy validate -> graceful reload)
# BEFORE it is stopped, and is only re-added (graceful reload back to both ports) AFTER its
# /healthz returns the expected instance name. The sibling always carries traffic.
#
# Topology:
#   GREEN :3000 earl-frontend.service       dir /home/rich/earl-knows-football/frontend
#   BLUE  :3001 earl-frontend-blue.service  dir /home/rich/earl-knows-football/frontend-blue
#   Source + node_modules are hardlink-SHARED between the two dirs; only .next differs
#   (that is precisely the file we replace). /healthz => {"instance":"green"|"blue"}.
#
# Usage (run on the frontend box, shell has sudo without password). BUILD and DEPLOY are
# SEPARATE commands — deploy NEVER rebuilds, so it takes SECONDS:
#   sudo /usr/local/bin/deploy-frontend build    # compile ONCE into /tmp/earl-build (minutes)
#                                                #   run only when source changed. Safe any time:
#                                                #   the build dir is isolated and never served.
#   sudo /usr/local/bin/deploy-frontend          # DEPLOY (seconds): copy artifact to both halves,
#                                                #   draining/healing each. No rebuild.
#   sudo /usr/local/bin/deploy-frontend green|blue  # DEPLOY to just one half (seconds, no rebuild)
#
# The source (changed .tsx/.css/... files) MUST already be uploaded into BOTH dirs before
# running `build` (scp the file into frontend/ and frontend-blue/, or write in-place to preserve
# the source hardlink). This script only builds ONCE and redeploys the SAME compiled artifact.
#
set -euo pipefail

ROOT="/home/rich/earl-knows-football"
GREEN_DIR="$ROOT/frontend";            GREEN_SVC="earl-frontend.service"
BLUE_DIR="$ROOT/frontend-blue";        BLUE_SVC="earl-frontend-blue.service"
BUILD_DIR="/tmp/earl-build"            # isolated hardlink clone; NEVER served
LOG="/tmp/earl-deploy.log"

echo "== deploy-frontend (build-once/run-same-both) ==" | tee "$LOG"

# healthz <port> <instance-name>  — wait up to ~60s for the named instance to pass.
healthz() {
  local port="$1" name="$2" body
  for _ in $(seq 1 30); do
    body="$(curl -sf --max-time 3 "http://localhost:$port/healthz" || true)"
    if printf '%s' "$body" | grep -q "\"instance\":\"$name\""; then
      echo "  OK :$port = $name healthy"; return 0
    fi
    sleep 2
  done
  echo "  FAIL: instance '$name' on :$port never healthy"; return 1
}

# drain <green|blue>  — remove that port from Caddy (peer carries), graceful reload.
drain() {
  local target="$1" d="/etc/caddy/Caddyfile.drain-$target"
  cp /etc/caddy/Caddyfile "$d"
  if [ "$target" = green ]; then
    sed -i 's#reverse_proxy localhost:3000 localhost:3001#reverse_proxy localhost:3001#' "$d"
    echo "  drain green -> leaving :3001 (blue)"
  else
    sed -i 's#reverse_proxy localhost:3000 localhost:3001#reverse_proxy localhost:3000#' "$d"
    echo "  drain blue -> leaving :3000 (green)"
  fi
  sudo caddy validate --config "$d"
  sudo caddy reload --config "$d"
  rm -f "$d"
}

# readd — restore rotation across both ports, graceful reload of the live file.
readd() { sudo caddy reload --config /etc/caddy/Caddyfile; }

# build_once — compile the site into the isolated scratch clone /tmp/earl-build.
# The clone shares source+node_modules with green via hardlink, but we DELETE its .next
# first because that .next is hardlink-shared with the LIVE green .next (cp -al) — building
# in-place over it would corrupt the live instance. Fresh isolated artifact instead.
build_once() {
  echo "== [1/2] BUILD-ONCE in isolated scratch $BUILD_DIR (no live instance touched) =="
  echo "-- making fresh isolated clone of source+node_modules --"
  rm -rf "$BUILD_DIR"
  if ! cp -al "$GREEN_DIR" "$BUILD_DIR"; then
    echo "  (hardlink clone failed - falling back to real copy)"
    rm -rf "$BUILD_DIR"; cp -a "$GREEN_DIR" "$BUILD_DIR"
  fi
  echo "-- deleting hardlink-shared .next from clone so the build writes fresh isolated bytes --"
  rm -rf "$BUILD_DIR/.next" "$BUILD_DIR/tsconfig.tsbuildinfo"  # <-- CRITICAL unlink from live
  rm -rf "$BUILD_DIR/.next_bak."* 2>/dev/null || true
  echo "-- next build (this is the ONE compile) --"
  ( cd "$BUILD_DIR" && NODE_ENV=production npm run build ) 2>&1 | tee -a "$LOG"
  local id; id="$(cat "$BUILD_DIR/.next/BUILD_ID" 2>/dev/null || echo UNKNOWN)"
  echo "-- canonical BUILD_ID = $id --"
}

# swap <green|blue>  — put ONE instance onto the canonical build in /tmp/earl-build.
swap() {
  local target="$1" dir svc peer peer_port port
  if [ "$target" = green ]; then dir="$GREEN_DIR"; svc="$GREEN_SVC"; peer=blue;  peer_port=3001; port=3000
  else                       dir="$BLUE_DIR";  svc="$BLUE_SVC"; peer=green; peer_port=3000; port=3001; fi
  echo "== [2/2] swap $target (:${port}) onto canonical build — $peer (:${peer_port}) serves during this =="

  echo "-- confirm sibling (:${peer_port}) healthy before touching $target --"
  healthz "$peer_port" "$peer"

  drain "$target"                       # 100% traffic now on $peer; nothing reaches $target
  echo "-- stop $svc --"
  sudo systemctl stop "$svc"

  echo "-- atomic .next swap into $dir (rsync --delete: also removes any stale hashed asset) --"
  # We can NOT cp -al from BUILD_DIR/.next because that would just hardlink green's own bytes
  # back onto itself via the shared source; a real recursive copy here is correct and safe.
  rm -rf "$dir/.next.prior"
  [ -d "$dir/.next" ] && mv "$dir/.next" "$dir/.next.prior"
  cp -a "$BUILD_DIR/.next" "$dir/.next"

  echo "-- start $svc --"
  sudo systemctl start "$svc"
  healthz "$port" "$target" || {
    echo "!! $target failed healthz after swap - rolling back to prior .next"
    sudo systemctl stop "$svc"
    rm -rf "$dir/.next"; [ -d "$dir/.next.prior" ] && mv "$dir/.next.prior" "$dir/.next"
    sudo systemctl start "$svc"
    readd
    return 1
  }
  readd                                        # restore rotation across both ports
  # confirm page serves
  if curl -sf -o /dev/null --max-time 10 "http://localhost:$port/mlb"; then
    echo "  OK :$port /mlb -> 200"
  else
    echo "  WARN: :$port /mlb not 200 (instance up but check route)"
  fi
  rm -rf "$dir/.next.prior"
  echo "== $target live on canonical build =="
}

verify_identical() {
  local g b
  g="$(cat "$GREEN_DIR/.next/BUILD_ID" 2>/dev/null || echo MISSING)"
  b="$(cat "$BLUE_DIR/.next/BUILD_ID"  2>/dev/null || echo MISSING)"
  echo "== verify both instances on the SAME build =="
  echo "  green BUILD_ID: $g"
  echo "  blue  BUILD_ID: $b"
  if [ "$g" = "$b" ] && [ "$g" != "MISSING" ]; then
    echo "  MATCH  ($g)"
  else
    echo "  !! BUILD_ID MISMATCH - investigate before calling it done"
    return 1
  fi
}

main() {
  local target="${1:-deploy}"
  case "$target" in
    deploy)
      # SECONDS: no rebuild - just put the already-built artifact on both instances.
      [ -d "$BUILD_DIR/.next" ] || { echo "!! no artifact at $BUILD_DIR/.next - run: $0 build"; exit 3; }
      swap blue
      swap green
      verify_identical
      ;;
    build)
      # MINUTES: compile once (only when source changes). Does NOT touch any live instance.
      build_once
      echo "Artifact ready: $BUILD_DIR/.next  -> deploy with: sudo $0"
      ;;
    green|blue)
      [ -d "$BUILD_DIR/.next" ] || { echo "!! no artifact at $BUILD_DIR/.next - run: $0 build"; exit 3; }
      swap "$target"
      ;;
    *)
      echo "usage: $0 [build]      # build ONCE into the artifact (minutes; when source changes)"
      echo "       $0              # deploy that build to both (seconds; no rebuild)"
      echo "       $0 green|blue   # deploy to a single instance (seconds; no rebuild)"
      exit 2 ;;
  esac

  echo
  echo "Done. Instances:"
  systemctl is-active earl-frontend.service       | sed 's/^/  green (3000): /'
  systemctl is-active earl-frontend-blue.service  | sed 's/^/  blue  (3001): /'
  echo "Public check: curl -sI https://earlknowsball.com/healthz"
}

main "$@"
