#!/usr/bin/env bash
# ============================================================
#  PixelAbs API — pull-based deploy
# ------------------------------------------------------------
#  GitHub cannot reach this VM: its public address is IPv6-only
#  (to avoid Azure's public-IPv4 charge) and GitHub-hosted
#  runners have no IPv6 egress. So the VM checks GitHub itself
#  and rebuilds only when there is something new to deploy.
#
#  Installed at /usr/local/bin/pixelabs-deploy and run by the
#  pixelabs-deploy.timer systemd unit every few minutes.
#
#  The container is published on the loopback interface only
#  (Caddy owns 80/443) — keep it that way.
#
#  What counts as "already deployed" is the SHA recorded in
#  STATE_FILE after a successful deploy — NOT the working tree's
#  HEAD. A manual `git pull` on the box used to advance HEAD and
#  make this script think the new code was live when it was not.
# ============================================================
set -euo pipefail

APP_DIR="/home/azureuser/pixelabs-api"
REPO="https://github.com/Vishalkumar-acad/pixelabs-api.git"
BRANCH="main"
APP_USER="azureuser"
HEALTH_URL="http://127.0.0.1:10000/health"
STATE_FILE="/var/lib/pixelabs-deploy/deployed"

log() { echo "[$(date -Is)] $*"; }
as_user() { sudo -u "$APP_USER" -H git -C "$APP_DIR" "$@"; }

# First run on a fresh VM: clone it.
if [ ! -d "$APP_DIR/.git" ]; then
  log "cloning $REPO"
  sudo -u "$APP_USER" -H git clone "$REPO" "$APP_DIR"
fi

as_user fetch --prune origin "$BRANCH" --quiet
REMOTE="$(as_user rev-parse "origin/$BRANCH")"
DEPLOYED="$(cat "$STATE_FILE" 2>/dev/null || echo none)"

if [ "$DEPLOYED" = "$REMOTE" ]; then
  exit 0                      # this exact commit is already running
fi

log "deploying ${REMOTE:0:7} (running: ${DEPLOYED:0:7})"
as_user reset --hard "$REMOTE"

cd "$APP_DIR"
docker build -t pixelabs-api .
docker rm -f api >/dev/null 2>&1 || true
docker run -d --name api --restart=always \
  -p 127.0.0.1:10000:10000 -e PORT=10000 -e GIT_SHA="$REMOTE" pixelabs-api >/dev/null
docker image prune -f >/dev/null

for _ in $(seq 1 15); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$STATE_FILE")"
    echo "$REMOTE" >"$STATE_FILE"      # only after the new build answers
    log "OK — deployed ${REMOTE:0:7}, /health is answering"
    exit 0
  fi
  sleep 2
done

log "health check FAILED after deploying ${REMOTE:0:7}"
docker logs --tail 40 api || true
exit 1                              # state file untouched -> retries next tick
