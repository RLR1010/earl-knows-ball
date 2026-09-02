#!/usr/bin/env bash
#
# deploy-frontend.sh — zero-downtime Next.js deploy for Earl Knows Ball (blue/green).
#
# Production frontend host: earlknowsball.com (Caddy) runs TWO Next.js instances
# behind a Caddy load balancer with /healthz health checks:
#     GREEN  3000  systemd earl-frontend.service        dir /home/rich/earl-knows-football/frontend
#     BLUE   3001  systemd earl-frontend-blue.service   dir /home/rich/earl-knows-football/frontend-blue
# Both dirs SHARE source via hardlinks (editing canonical frontend/ is reflected in
# frontend-blue/ — see the clone recipe at the bottom). Each has its OWN .next build.
#
# The danger this script prevents: `next build` writes .next WHILE a running `next start`
# is serving from it → users get 50x / half-built assets. To never rebuild a live instance,
# we take the target OUT of rotation (stop its unit) so Caddy sends 100% to the sibling,
# build it, health-check it, then bring it back.
#
# Usage (run on the frontend box, as rich):
#   sudo ./deploy-frontend.sh                 # deploy BOTH: build blue (drain to green),
#                                             #   then green (drain to blue). ~2 x next build.
#   sudo ./deploy-frontend.sh green           # rebuild only GREEN  (blue serves the whole time)
#   sudo ./deploy-frontend.sh blue            # rebuild only BLUE   (green serves the whole time)
#
# The source must ALREADY be on the box (scp/rsync first). new files: re-run the hardlink
# clone in the recipe below or copy new files into BOTH dirs.
#
set -euo pipefail

ROOT="/home/rich/earl-knows-football"
GREEN_DIR="$ROOT/frontend"
BLUE_DIR="$ROOT/frontend-blue"
GREEN_SVC="earl-frontend.service"
BLUE_SVC="earl-frontend-blue.service"

# Health check helpers ---------------------------------------------------------
healthz() {  # $1 = port, $2 = expected instance name
  local port="$1" name="$2" body
  for _ in $(seq 1 30); do
    body="$(curl -sf --max-time 3 "http://localhost:$port/healthz" || true)"
    if printf '%s' "$body" | grep -q "\"instance\":\"$name\""; then
      echo "  OK  :$port = $name healthy"; return 0
    fi
    sleep 2
  done
  echo "  FAIL: instance '$name' on :$port never became healthy"
  return 1
}

# Build + deploy a single target with TRUE zero-downtime drainage ------------------
# We drain the target from Caddy FIRST (so no request ever reaches it while stopped),
# rebuild it, health-check it, then reload Caddy to include it again. Caddy reload is
# graceful/zero-downtime, so the site never drops a request.
rebuild_target() {   # $1 = green|blue
  local target="$1" dir svc
  if [ "$target" = "green" ]; then
    dir="$GREEN_DIR"; svc="$GREEN_SVC"; peer="blue"
  elif [ "$target" = "blue" ]; then
    dir="$BLUE_DIR"; svc="$BLUE_SVC"; peer="green"
  else
    echo "unknown target: $target (expected green|blue)"; return 1
  fi
  # peer (not being built) serves during the rebuild.
  local peer_port=3000; [ "$target" = "green" ] && peer_port=3001

  echo "== Rebuilding $target ($dir) — $peer (:${peer_port}) carries traffic =="

  echo "-- ensure sibling (:${peer_port}) is healthy before we touch $target --"
  healthz "$peer_port" "$peer"

  echo "-- drain $target from Caddy (route :${peer_port} only), graceful reload --"
  # Surgical: the frontend_lb snippet's upstream is a single line listing both ports.
  local live="/etc/caddy/Caddyfile" drain="/etc/caddy/Caddyfile.drain-$target"
  cp "$live" "$drain"
  if [ "$target" = "green" ]; then
    sed -i 's/reverse_proxy localhost:3000 localhost:3001/reverse_proxy localhost:3001/' "$drain"
  else
    sed -i 's/reverse_proxy localhost:3000 localhost:3001/reverse_proxy localhost:3000/' "$drain"
  fi
  sudo caddy validate --config "$drain"   # throw before we ever touch a live service
  sudo caddy reload --config "$drain"
  rm -f "$drain"

  echo "-- stop $svc ($target out of rotation, zero requests reaching it) --"
  sudo systemctl stop "$svc"

  echo "-- build (NODE_ENV=production) in $dir --"
  ( cd "$dir" && NODE_ENV=production npm run build )

  echo "-- start $svc --"
  sudo systemctl start "$svc"
  local port; [ "$target" = "green" ] && port=3000 || port=3001
  healthz "$port" "$target"

  echo "-- re-add $target to Caddy (both :3000/:3001), graceful reload --"
  sudo caddy reload --config "$live"

  echo "-- verify target serves pages --"
  curl -sf -o /dev/null --max-time 10 "http://localhost:$port/mlb" && echo "  OK  :$port /mlb -> 200"

  echo "== $target rebuilt and healthy =="
}

main() {
  local target="${1:-both}"
  case "$target" in
    both)
      rebuild_target blue     # blue first: green(old) serves; then green drains to blue(new)
      rebuild_target green
      ;;
    green|blue) rebuild_target "$target" ;;
    *) echo "usage: $0 [green|blue]   (default: both)"; exit 2 ;;
  esac

  echo
  echo "Done. Instances:"
  systemctl is-active earl-frontend.service        | sed 's/^/  green (3000): /'
  systemctl is-active earl-frontend-blue.service   | sed 's/^/  blue  (3001): /'
  echo "Public check: curl -sI https://earlknowsball.com/healthz"
}

main "$@"
