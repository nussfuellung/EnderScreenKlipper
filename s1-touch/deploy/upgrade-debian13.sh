#!/usr/bin/env bash
# Hebt einen Debian-12-LXC (bookworm) auf Debian 13 (trixie) an – nötig für aktuelle OrcaSlicer-Builds.
# Vorher in Proxmox einen Snapshot/Backup vom Container machen (Container → Snapshots → Take Snapshot)!
set -euo pipefail
. /etc/os-release
if [ "${VERSION_CODENAME:-}" = "trixie" ]; then echo "Schon Debian 13 – nichts zu tun."; exit 0; fi
[ "${VERSION_CODENAME:-}" = "bookworm" ] || { echo "Erwartet Debian 12 (bookworm), gefunden: $PRETTY_NAME"; exit 1; }
echo "Upgrade Debian 12 → 13. Dauert ein paar Minuten …"
export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get -y upgrade
sed -i 's/bookworm/trixie/g' /etc/apt/sources.list 2>/dev/null || true
find /etc/apt/sources.list.d -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i 's/bookworm/trixie/g' {} + 2>/dev/null || true
apt-get update
apt-get -y -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" full-upgrade
apt-get -y autoremove --purge
echo
echo "Fertig: $(. /etc/os-release; echo $PRETTY_NAME)."
echo "Jetzt den Container neu starten (auf dem Proxmox-Host: pct reboot 120) und danach install-mobile.sh erneut ausführen."
