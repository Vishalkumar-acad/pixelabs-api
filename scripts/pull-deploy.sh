#!/usr/bin/env bash
# ============================================================
#  PixelAbs API — pull-based deploy
# ------------------------------------------------------------
#  GitHub can no longer reach this VM: its public address is
#  IPv6-only (to avoid Azure's public-IPv4 charge) and
#  GitHub-hosted runners have no IPv6 egress, so the SSH deploy
#  fails. Instead of opening a port, the VM checks GitHub itself
#  and rebuilds only when main has actually moved.
#
#  Installed at /usr/local/bin/pixelabs-deploy and run by the
#  pixelabs-deploy.timer systemd unit every few minutes.
#
#  The container is published on the loopback interface only
#  (Caddy owns 80/443) — keep it that way.
# ============================================================
set -euo pipefail

APP_DIR="/home/azureuser/pixelabs-api"
REPO="https://github.com/Vishalkumar-acad/pixelabs-api.git"
BRANCH="main"
APP_USER="azureuser"
HEALTH_URL="http://127.0.0.1:10000/health"

log() { echo "[$(date -Is)] $*"; }
as_user() { sudo -u "$APP_USER" -H git -C "$APP_DIR" "$@"; }

# First run on a fresh VM: clone it.
if [ ! -d "$APP_DIR/.git" ]; then
  log "cloning $REPO"
  sudo -u "$APP_USER" -H git clone "$REPO" "$APP_DIR"
fi

as_user fetch --prune origin "$BRANCH" --quiet
LOCAL="$(as_user rev-parse HEAD)"
REMOTE="$(as_user rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0                      # nothing new — stay quiet
fi

log "new commit ${REMOTE:0:7} (was ${LOCAL:0:7}) — deploying"
as_user reset --hard "origin/$BRANCH"

cd "$APP_DIR"
docker build -t pixelabs-api .
docker rm -f api >/dev/null 2>&1 || true
docker run -d --name api --restart=always \
  -p 127.0.0.1:10000:10000 -e PORT=10000 pixelabs-api >/dev/null
docker image prune -f >/dev/null

for _ in $(seq 1 15); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    log "OK — deployed ${REMOTE:0:7}, /health is answering"
    exit 0
  fi
  sleep 2
done

log "health check FAILED after deploying ${REMOTE:0:7}"
docker logs --tail 40 api || true
exit 1
