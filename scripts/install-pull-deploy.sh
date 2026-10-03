#!/usr/bin/env bash
# ============================================================
#  One-shot installer for the pull-based deploy.
#
#  On the VM, as root:
#      curl -fsSL https://raw.githubusercontent.com/Vishalkumar-acad/pixelabs-api/main/scripts/install-pull-deploy.sh | sudo bash
#
#  Installs /usr/local/bin/pixelabs-deploy plus its systemd
#  timer, so the VM checks GitHub every few minutes and rebuilds
#  only when main has moved. Nothing connects *in* to the VM.
# ============================================================
set -euo pipefail

RAW="https://raw.githubusercontent.com/Vishalkumar-acad/pixelabs-api/main/scripts/pull-deploy.sh"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root:  curl -fsSL <this script> | sudo bash" >&2
  exit 1
fi

echo "→ installing /usr/local/bin/pixelabs-deploy"
curl -fsSL "$RAW" -o /usr/local/bin/pixelabs-deploy
chmod 755 /usr/local/bin/pixelabs-deploy

echo "→ writing the systemd unit + timer"
cat >/etc/systemd/system/pixelabs-deploy.service <<'UNIT'
[Unit]
Description=PixelAbs API pull-deploy (rebuild when main changes)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pixelabs-deploy
UNIT

cat >/etc/systemd/system/pixelabs-deploy.timer <<'UNIT'
[Unit]
Description=Run the PixelAbs pull-deploy every 3 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now pixelabs-deploy.timer

echo "→ running it once now"
systemctl start pixelabs-deploy.service || true

echo
echo "Timer:"
systemctl list-timers pixelabs-deploy.timer --no-pager | head -3 || true
echo
echo "Last run:"
journalctl -u pixelabs-deploy -n 15 --no-pager || true
echo
echo "Done. Push to main and it will be live within ~3 minutes."
