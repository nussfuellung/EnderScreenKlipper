#!/usr/bin/env bash
# Holt die neueste Version aus dem Git-Repo und aktualisiert Tablet- und Handy-App.
#   bash deploy/update.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$DIR/.." pull --ff-only 2>/dev/null || git -C "$DIR" pull --ff-only
[ -d /var/www/s1-touch ] && cp "$DIR/web/index.html" /var/www/s1-touch/ && echo "Tablet-App aktualisiert"
if [ -d /var/www/s1-mobile ]; then
  cp -r "$DIR/mobile/." /var/www/s1-mobile/ && cp "$DIR/server/s1mobile.py" /opt/s1-mobile/ && systemctl restart s1-mobile && echo "Handy-App + Backend aktualisiert"
fi
