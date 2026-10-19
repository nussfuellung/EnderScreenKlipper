# S1 Touch – Bambu-Style Touch-Oberfläche für den Ender-3 S1 (Klipper)

Eine einzelne Web-App (`web/index.html`, keine Abhängigkeiten, kein Build), die per Moonraker-WebSocket live mit Klipper spricht. Gedacht für ein Tablet im Querformat am Drucker.

```
s1-touch/
├── web/index.html            ← die komplette App
├── klipper/s1_touch.cfg      ← Makros & Sektionen für Klipper
├── klipper/moonraker_power.conf ← optional: Netzteil/Licht über Moonraker + HA
├── mobile/                   ← Handy-App „S1 Mobile“ (PWA)
├── server/s1mobile.py        ← Slicer-/Printables-Backend für die Handy-App
└── deploy/
    ├── docker-compose.yml    ← Variante A: Docker (nginx + Proxy)
    ├── .env.example          ← Klipper-IP, Home-Assistant-URL & Token
    ├── nginx.conf
    ├── install-lxc.sh        ← Variante B: Debian-LXC ohne Docker
    ├── install-mobile.sh     ← Handy-App + OrcaSlicer im selben LXC (Port 8081)
    ├── nginx-mobile.conf, s1-mobile.service
    └── update.sh             ← git pull + beide Apps aktualisieren
```

## Funktionen

| Bereich | Inhalt |
|---|---|
| **Start** | Begrüßung mit stilisiertem Ender-3 S1 · Druckdateien · Status (Düse, Bett, Filamentsensor, Verbindung). Während des Drucks: Vorschaubild, Fortschritt, Schicht x/y, Restzeit, **Überspringen** (Objekte ausschließen), **Pause/Fortsetzen**, **Stopp**, **Feinjustierung** (Babystep, Speed, Fluss, Lüfter), Umschalten auf Webcam |
| **Steuerung** | Kacheln wie Bambu: Lüfter, Geschwindigkeit (Leise/Standard/Sport/Turbo), Bewegung (Jog + Home), Düse & Extruder, Elektronik-Temperatur, Heizbett, Licht. Die Illustration zeigt die echte Kopf-/Gantry-/Bettposition und glüht beim Heizen |
| **Filament** | Material & Farbe wählen, **Laden**, **Entladen**, **Wechseln (M600)**, Vorheizen |
| **Kalibrierung** | **Z-Offset-Assistent** (PROBE_CALIBRATE mit Papier, wie Pro-Firmware) + Live-Babystep · **Bed Mesh** (Heatmap, Profile) · **Tramming-Assistent** (mit CR-Touch über `SCREWS_TILT_CALCULATE` *oder* Papier-Methode Ecke für Ecke) · PID-Tuning · SAVE_CONFIG · Filamentsensor an/aus · Not-Aus · Neustarts |
| **Schalter** | Licht & Netzteil aus **Home Assistant**, Moonraker `[power]` oder Klipper – auf der Steuerungsseite, auf der Startseite (Netzteil) und in der Kachel *Schalter* (inkl. beliebig vieler weiterer Schalter). Ist der Drucker aus, zeigt die App „Drucker einschalten“. Ausschalten bei heißer Düse erst nach Abkühlen auf < 50 °C, optional automatisch nach Druckende |
| **Meldungen** | Konsole mit Klipper-Antworten, Fehler-Badge, Schnellbefehle |

Ohne Verbindung startet die App im **Demo-Modus** – alle Seiten und Assistenten lassen sich so vorab durchklicken.

## 1. Klipper vorbereiten

1. `klipper/s1_touch.cfg` neben die `printer.cfg` legen und in der `printer.cfg` einbinden:
   ```ini
   [include s1_touch.cfg]
   ```
2. **Prüfe die Schraubenpositionen** in `[screws_tilt_adjust]`. Sie sind aus Klippers S1-Referenzconfig berechnet (Schrauben bei 20/29 … 195/198, CR-Touch-Offset −31,8 / −40,5). Wenn dein Probe-Offset anders ist: `screwN = Schraubenposition − Offset`.
3. Für **Objekte überspringen**: im Slicer *Objekte beschriften* (Orca/Prusa: „Label objects“, Firmware-Typ Klipper) aktivieren. Moonraker braucht in der `moonraker.conf`:
   ```ini
   [file_manager]
   enable_object_processing: True
   ```
4. **Licht & Netzteil:** siehe Abschnitt *Home Assistant* unten.

## 2. Auf Proxmox hosten

Empfohlen ist der **Proxy-Betrieb**: nginx liefert die App aus und leitet `/websocket`, `/printer`, `/server` … an Moonraker weiter. Das Tablet braucht dann nur eine Adresse und es gibt keine CORS-Probleme.

**Variante A – Docker** (LXC/VM mit Docker):
```bash
cd s1-touch/deploy
# MOONRAKER_HOST in docker-compose.yml auf die IP deines Klipper-Pis setzen
docker compose up -d
# → http://<proxmox-ip>:8080/?host=proxy
```

**Variante B – schlanker Debian-LXC ohne Docker:**
```bash
sudo ./deploy/install-lxc.sh 192.168.1.50   # IP des Klipper-Hosts
# → http://<lxc-ip>/?host=proxy
```

**Moonraker freigeben** – in `moonraker.conf` die IP bzw. das Netz des Proxmox-Containers zulassen:
```ini
[authorization]
trusted_clients:
    192.168.1.0/24
cors_domains:
    http://<lxc-ip>
    http://<lxc-ip>:8080
```
(`cors_domains` ist nur nötig, wenn du die App *ohne* Proxy direkt mit `?host=<pi-ip>` nutzt.)

Die Verbindung lässt sich jederzeit in der App unter *Kalibrierung → Einstellungen* ändern (Host, Port, Webcam, Licht). Webcam über Crowsnest: `/webcam/?action=stream`.

## Home Assistant: Licht & Netzteil

Es gibt zwei Wege – beide lassen sich auch mischen:

**Weg 1 – Home Assistant direkt (über den nginx-Proxy)**
1. In HA: Profil → Sicherheit → *Langlebiges Zugriffstoken* erstellen (am besten für einen eigenen Nicht-Admin-Benutzer „drucker“).
2. URL und Token in `deploy/.env` eintragen (`cp .env.example .env`) bzw. beim LXC-Skript als 2./3. Parameter übergeben.
3. In der App: *Kalibrierung → Einstellungen → Home Assistant* = `proxy`, „Verbindung testen“ → die Felder schlagen dann alle `switch.`/`light.`/`fan.`/`input_boolean.`-Entitäten vor.
4. Unter *Schalter*: Licht = „Home Assistant“ + z. B. `light.drucker_licht`, Netzteil = „Home Assistant“ + z. B. `switch.drucker_steckdose`.

Der Token bleibt im Proxy, das Tablet sieht ihn nie. Der Proxy lässt nur Zustände lesen und `homeassistant.turn_on/turn_off/toggle` durch – also keine Automationen, keine Konfiguration. Wer das Panel im Netz erreicht, kann damit trotzdem *alle* Entitäten schalten, die der HA-Benutzer schalten darf. Ohne Proxy kann man auch die HA-Adresse direkt + Token in der App eintragen; dann muss in HA `http: cors_allowed_origins` die Panel-Adresse enthalten und der Token liegt im Tablet-Browser.

**Weg 2 – über Moonraker `[power]`** (empfohlen fürs Netzteil)
`klipper/moonraker_power.conf` in die `moonraker.conf` übernehmen. Moonraker sperrt das Netzteil dann während des Drucks, startet Klipper nach dem Einschalten neu und schaltet bei Bedarf ein, wenn du aus dem Slicer einen Druck schickst. Die App findet Geräte namens `drucker`/`psu`/`netzteil` bzw. `licht`/`light` automatisch (Einstellung „Automatisch“); alle weiteren `[power]`-Geräte erscheinen in der Kachel *Schalter*.

**Wichtig:** Der Klipper-Host (Pi) muss eigenen Strom haben – hängt er an derselben Steckdose, kann er sich nicht wieder einschalten. Nach dem Einschalten löst die App nach ~8 s selbst einen Firmware-Neustart aus, falls Klipper noch nicht bereit ist.

## 3. Tablet einrichten

- Android: Chrome öffnen → Seite aufrufen → ⋮ → *Zum Startbildschirm hinzufügen* → startet dann im Vollbild. Für echten Kiosk-Betrieb z. B. *Fully Kiosk Browser* (Bildschirm-An, Autostart, Bewegung weckt Display).
- iPad: Safari → Teilen → *Zum Home-Bildschirm*; *Geführter Zugriff* sperrt das Gerät auf die App.
- Optimiert für 16:10/16:9 im Querformat (7–11"). Alle Größen skalieren mit der Bildschirmhöhe.
- Die App fordert einen Wake-Lock an, damit das Display während des Drucks an bleibt (nur über HTTPS oder als installierte App zuverlässig – sonst im Kiosk-Browser einstellen).

## Hinweise

- Kalibrier-Ergebnisse (Z-Offset, Mesh, PID) liegen bis zum **Speichern** nur im RAM. Ein grüner Hinweis oben rechts erinnert daran; *Speichern* führt `SAVE_CONFIG` aus (Klipper startet neu).
- Jog-Tasten funktionieren erst nach dem Referenzieren und sind während des Drucks gesperrt.
- Für die Restzeit nutzt die App Fortschritt + Slicer-Schätzung; Schichtanzeige über `SET_PRINT_STATS_INFO` (Orca/Prusa: in den Schichtwechsel-G-Code `SET_PRINT_STATS_INFO CURRENT_LAYER={layer_num + 1}` und in den Start-G-Code `SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]`), sonst wird sie aus der Z-Höhe geschätzt.


---

# S1 Mobile – Handy-App (wie Bambu Handy)

Läuft im **selben Container** auf Port **8081**, als installierbare Web-App (PWA).

| Tab | Inhalt |
|---|---|
| **Drucker** | Live-Kamera (oder animierter Ender-3 S1), Druckfortschritt mit Vorschaubild, Pause/Stopp, Temperaturen, Lüfter, Tempo, Licht, Netzteil, Bewegung, Filament laden/entladen, Druckdateien, Druckverlauf |
| **Entdecken** | Printables durchsuchen (Beliebt/Neu/Top), Modell-Details mit Bildern und Dateien → **„Slicen“** lädt die Datei auf deinen Server |
| **Projekte** | Eigene Modelle (STL, 3MF, OBJ, STEP, ZIP) hochladen, Slice-Aufträge, fertige G-Codes drucken |
| **Slicen** | 3D-Vorschau auf dem 220×220-Bett, Profil (Drucker/Qualität/Filament), Stützen Aus/Normal/Baum + „nur auf Druckbett“, Infill, Schichthöhe, Wände, Brim, Größe, Auto-Ausrichten, Temperaturen → **OrcaSlicer-CLI** auf dem Server → Druckzeit/Filament → **Drucken** oder nur **Senden** |
| **Ich** | Profil vom PC importieren, Verbindung, Kamera, HA-Licht/-Netzteil, Hell/Dunkel, Benachrichtigung bei Druckende, Demo-Modus |

## Installation

0. **Debian 13 nötig:** Aktuelle OrcaSlicer-Versionen gibt es nur noch für Ubuntu 24.04 – das läuft auf Debian 13, nicht auf Debian 12. Ein Debian-12-Container lässt sich mit `bash s1-touch/deploy/upgrade-debian13.sh` anheben (vorher Snapshot machen, danach `pct reboot 120`).
1. **Container vergrößern** (auf dem Proxmox-Host – OrcaSlicer braucht RAM und Platz):
   ```bash
   pct set 120 --memory 3072 --swap 2048
   pct resize 120 rootfs +6G
   ```
2. **Im Container:**
   ```bash
   cd /root/esk && git pull
   bash s1-touch/deploy/install-mobile.sh 192.168.188.XX                     # nur Klipper
   bash s1-touch/deploy/install-mobile.sh 192.168.188.XX http://HA-IP:8123 'TOKEN'   # mit Home Assistant
   ```
   Das Skript lädt die aktuelle OrcaSlicer-AppImage von GitHub, entpackt sie nach `/opt/orcaslicer`, richtet den Dienst `s1-mobile` ein und startet die App auf `http://<container-ip>:8081/`.
3. **Updates** später: `bash /root/esk/s1-touch/deploy/update.sh`. OrcaSlicer neu laden: `ORCA_UPDATE=1 bash …/install-mobile.sh …`.

## Profil vom PC übernehmen

Die App nutzt deine **eigenen Orca-Profile** (mit Klipper-Startcode `PRINT_START`). Bis dahin stehen die Orca-Systemprofile für den Ender-3 S1 bereit – die haben aber Marlin-Startcode.

- **Einmalig, am einfachsten:** In OrcaSlicer am PC Drucker + Prozess + Filament(e) wählen → *Datei → Projekt speichern* → die `.3mf` in der App unter *Ich → Profil importieren* hochladen. Alles wird vollständig übernommen (auch mehrere Filamente).
- **Alle eigenen Profile:** *Datei → Exportieren → Preset-Bundle exportieren* → `.orca_printer` importieren. Vererbung von Systemprofilen löst der Server selbst auf.
- **Automatisch synchron:** Den Orca-Benutzerordner (`%APPDATA%\OrcaSlicer\user`) per **Syncthing** oder SMB-Freigabe nach `/var/lib/s1-mobile/profiles/sync` spiegeln – Änderungen am PC sind sofort in der App.

## Printables

Printables hat keine offizielle API. Die App nutzt dieselbe Schnittstelle wie die Website (über den Server, nicht vom Handy aus). Das kann sich jederzeit ändern – dann funktionieren Suche/Download nicht mehr, alles andere schon. Lizenz der Modelle beachten. Als installierte App kannst du Printables-Links per **Teilen → S1 Mobile** öffnen.

## Als App installieren, HTTPS & unterwegs

- Browser-Apps lassen sich nur über **HTTPS** richtig installieren (Service Worker, Teilen-Ziel, Benachrichtigungen). Per `http://` funktioniert alles, nur eben als Lesezeichen.
- Mit deinem **Caddy**-Container geht HTTPS am einfachsten: z. B. `s1.deine-domain.de` mit DNS-Challenge oder ein interner Name mit `tls internal` (dann das Caddy-Root-Zertifikat aufs Handy).
  ```
  s1.deine-domain.de {
      reverse_proxy 192.168.188.71:8081
  }
  ```
- **Von unterwegs** nicht per Portfreigabe ins Internet stellen (Moonraker hat dann keinen Schutz), sondern über **Tailscale** oder **WireGuard** ins Heimnetz.

## Fehlersuche

- `journalctl -u s1-mobile -f` – Server-Log; schlägt Slicen fehl, zeigt die App das Orca-Log unter *Projekte → Auftrag antippen*.
- „OrcaSlicer startet nicht“ beim Installieren: fehlende Bibliotheken werden angezeigt → mit `apt install` nachinstallieren.
- Moonraker lehnt den Upload ab → Container-IP unter `trusted_clients` in `moonraker.conf` eintragen.
