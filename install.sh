#!/usr/bin/env bash
# Usage: sudo bash install.sh USER https://github.com/OWNER/REPO.git [BRANCH]
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.'; exit 1; }
OWNER="${1:?Supply your Pi login username}"
REPOSITORY="${2:?Supply the GitHub repository URL}"
BRANCH="${3:-main}"
id "$OWNER" >/dev/null
[[ "$OWNER" != root ]] || { echo 'Use your ordinary Pi login username.'; exit 1; }
[[ "$REPOSITORY" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]] || { echo 'Use an HTTPS GitHub repository URL.'; exit 1; }
git check-ref-format --branch "$BRANCH" >/dev/null
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
apt-get update
apt-get install -y python3-picamera2 python3-gpiozero python3-lgpio git avahi-daemon
python3 -m unittest discover -s "$SOURCE/tests" -t "$SOURCE"
usermod -aG video,gpio "$OWNER"
RELEASE="/opt/pi-dashboard/releases/$(date +%Y%m%d%H%M%S)-$$"
install -d "$RELEASE" /usr/local/lib/pi-dashboard
cp -a "$SOURCE/." "$RELEASE/"
chown -R root:root "$RELEASE"
git -C "$SOURCE" rev-parse HEAD > "$RELEASE/REVISION" 2>/dev/null || echo local > "$RELEASE/REVISION"
install -m 0644 "$SOURCE/update_worker.py" /usr/local/lib/pi-dashboard/update_worker.py
python3 - "$OWNER" "$REPOSITORY" "$BRANCH" <<'PY'
import json, pathlib, sys
path = pathlib.Path('/etc/pi-dashboard.json')
path.write_text(json.dumps(dict(zip(('user', 'repository', 'branch'), sys.argv[1:]))))
path.chmod(0o644)
PY
ln -sfn "$RELEASE" /opt/pi-dashboard/current-new
mv -Tf /opt/pi-dashboard/current-new /opt/pi-dashboard/current
cat > /etc/systemd/system/pi-dashboard.service <<EOF
[Unit]
Description=Pi camera and cooling dashboard
After=network.target
[Service]
Type=simple
User=$OWNER
SupplementaryGroups=video gpio
WorkingDirectory=/opt/pi-dashboard/current
ExecStart=/usr/bin/python3 /opt/pi-dashboard/current/app.py
Restart=always
RestartSec=3
Environment=PYTHONDONTWRITEBYTECODE=1
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable avahi-daemon pi-dashboard.service
systemctl start avahi-daemon
systemctl restart pi-dashboard.service
echo "Open http://$(hostname).local:8080 on the same Wi-Fi."
