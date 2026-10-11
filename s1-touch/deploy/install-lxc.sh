#!/usr/bin/env bash
# S1 Touch ohne Docker in einem Debian/Ubuntu-LXC auf Proxmox installieren.
#   sudo ./deploy/install-lxc.sh <Klipper-IP> [Home-Assistant-URL] [HA-Token]
#   z.B. sudo ./deploy/install-lxc.sh 192.168.1.50 http://192.168.1.10:8123 eyJhbGciOi...
set -euo pipefail
export MOONRAKER_HOST="${1:?Bitte IP des Klipper-Hosts angeben}"
export HA_URL="${2:-}" HA_TOKEN="${3:-}"
export RESOLVER="$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)"; RESOLVER="${RESOLVER:-1.1.1.1}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
apt-get update && apt-get install -y nginx gettext-base
mkdir -p /var/www/s1-touch && cp "$DIR/web/index.html" /var/www/s1-touch/
envsubst '${MOONRAKER_HOST} ${HA_URL} ${HA_TOKEN} ${RESOLVER}' < "$DIR/deploy/nginx.conf" \
  | sed 's#/usr/share/nginx/html#/var/www/s1-touch#' > /etc/nginx/sites-available/s1-touch
chmod 600 /etc/nginx/sites-available/s1-touch
ln -sf /etc/nginx/sites-available/s1-touch /etc/nginx/sites-enabled/s1-touch
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "Fertig: http://$(hostname -I | awk '{print $1}')/?host=proxy"
