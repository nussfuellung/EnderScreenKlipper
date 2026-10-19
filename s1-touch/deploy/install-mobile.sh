#!/usr/bin/env bash
# S1 Mobile (Handy-App + Slicer-Server) im selben LXC wie die Tablet-Oberfläche installieren.
#   sudo bash deploy/install-mobile.sh <Klipper-IP> [Home-Assistant-URL] [HA-Token]
# Optional: ORCA_URL=<AppImage-URL> für eine bestimmte OrcaSlicer-Version, MOBILE_PORT=8081
set -euo pipefail
export MOONRAKER_HOST="${1:?Bitte IP des Klipper-Hosts angeben}"
export HA_URL="${2:-}" HA_TOKEN="${3:-}"
export MOBILE_PORT="${MOBILE_PORT:-8081}"
export RESOLVER="$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)"; RESOLVER="${RESOLVER:-1.1.1.1}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
say(){ printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

say "Speicher prüfen"
MEM=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
DISK=$(df -m / | awk 'NR==2{print $4}')
echo "RAM: ${MEM} MB, frei auf /: ${DISK} MB"
[ "$MEM" -lt 1800 ] && echo "WARNUNG: Für OrcaSlicer werden ≥2 GB RAM empfohlen (auf dem Proxmox-Host: pct set <ID> --memory 3072 --swap 2048)"
[ "$DISK" -lt 2500 ] && { echo "FEHLER: Mindestens 2,5 GB frei nötig (auf dem Host: pct resize <ID> rootfs +6G)"; exit 1; }

say "Pakete installieren"
apt-get update
apt-get install -y --no-install-recommends python3 nginx gettext-base curl ca-certificates jq xvfb xauth fonts-dejavu-core
# Laufzeit-Bibliotheken für OrcaSlicer – Namen unterscheiden sich je nach Debian-Version (z.B. *t64 ab Debian 13)
for p in libgtk-3-0 libgtk-3-0t64 libgl1 libegl1 libglu1-mesa libosmesa6 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
         libsecret-1-0 libnotify4 libfuse2 libfuse2t64 libwebkit2gtk-4.1-0 libwebkit2gtk-4.0-37; do
  apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 && echo "  ✓ $p" || true
done

say "OrcaSlicer herunterladen"
if [ ! -x /opt/orcaslicer/AppRun ] || [ "${ORCA_UPDATE:-0}" = 1 ]; then
  if [ -z "${ORCA_URL:-}" ]; then
    GLIBC=$(ldd --version | awk 'NR==1{print $NF}')
    REL=$(curl -fsSL https://api.github.com/repos/OrcaSlicer/OrcaSlicer/releases/latest)
    ORCA_VER=$(echo "$REL" | jq -r .tag_name)
    # Ubuntu-22.04-Build läuft auf Debian 12 (glibc 2.36); 24.04-Build braucht glibc ≥ 2.39
    ORCA_URL=$(echo "$REL" | jq -r '.assets[].browser_download_url' | grep -iE 'linux.*appimage$' | grep -i 2204 | head -1)
    if [ -z "$ORCA_URL" ] || awk "BEGIN{exit !($GLIBC >= 2.39)}"; then
      ORCA_URL=$(echo "$REL" | jq -r '.assets[].browser_download_url' | grep -iE 'linux.*appimage$' | grep -i "${ORCA_PREFER:-2404}" | head -1)
      [ -z "$ORCA_URL" ] && ORCA_URL=$(echo "$REL" | jq -r '.assets[].browser_download_url' | grep -iE 'linux.*appimage$' | head -1)
    fi
  fi
  [ -z "$ORCA_URL" ] && { echo "Keine OrcaSlicer-AppImage gefunden – ORCA_URL=... setzen"; exit 1; }
  echo "Lade $ORCA_URL"
  TMP=$(mktemp -d); curl -fL --progress-bar -o "$TMP/orca.AppImage" "$ORCA_URL"
  chmod +x "$TMP/orca.AppImage"; (cd "$TMP" && ./orca.AppImage --appimage-extract >/dev/null)
  rm -rf /opt/orcaslicer && mv "$TMP/squashfs-root" /opt/orcaslicer && rm -rf "$TMP"
  mkdir -p /var/lib/s1-mobile; echo "${ORCA_VER:-$(basename "$ORCA_URL")}" > /var/lib/s1-mobile/orca_version.txt
fi
echo "Teste OrcaSlicer-CLI …"
if xvfb-run -a /opt/orcaslicer/AppRun --help >/tmp/orca-help.txt 2>&1 || grep -qi "usage" /tmp/orca-help.txt; then
  echo "OK: $(grep -m1 -iE 'orca|version' /tmp/orca-help.txt || echo CLI antwortet)"
else
  echo "WARNUNG: OrcaSlicer startet nicht. Fehlende Bibliotheken:"; ldd /opt/orcaslicer/bin/* 2>/dev/null | grep "not found" | sort -u || tail -5 /tmp/orca-help.txt
fi

say "Backend einrichten"
id s1mobile >/dev/null 2>&1 || useradd --system --home /var/lib/s1-mobile --shell /usr/sbin/nologin s1mobile
mkdir -p /opt/s1-mobile /var/lib/s1-mobile/profiles/sync
cp "$DIR/server/s1mobile.py" /opt/s1-mobile/
chown -R s1mobile:s1mobile /var/lib/s1-mobile
cat > /etc/s1-mobile.env <<ENV
S1M_DATA=/var/lib/s1-mobile
S1M_PORT=8090
S1M_MOONRAKER=http://${MOONRAKER_HOST}:7125
S1M_ORCA=/opt/orcaslicer/AppRun
S1M_XVFB=auto
ENV
cp "$DIR/deploy/s1-mobile.service" /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now s1-mobile && systemctl restart s1-mobile

say "App & nginx"
rm -rf /var/www/s1-mobile && mkdir -p /var/www/s1-mobile && cp -r "$DIR/mobile/." /var/www/s1-mobile/
envsubst '${MOONRAKER_HOST} ${HA_URL} ${HA_TOKEN} ${RESOLVER} ${MOBILE_PORT}' < "$DIR/deploy/nginx-mobile.conf" > /etc/nginx/sites-available/s1-mobile
chmod 600 /etc/nginx/sites-available/s1-mobile
ln -sf /etc/nginx/sites-available/s1-mobile /etc/nginx/sites-enabled/s1-mobile
nginx -t && systemctl reload nginx || systemctl restart nginx

IP=$(hostname -I | awk '{print $1}')
say "Fertig"
echo "Handy-App:  http://${IP}:${MOBILE_PORT}/"
echo "Sync-Ordner für Orca-Profile: /var/lib/s1-mobile/profiles/sync"
echo "Logs: journalctl -u s1-mobile -f"
