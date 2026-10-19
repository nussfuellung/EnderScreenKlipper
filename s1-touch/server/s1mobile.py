#!/usr/bin/env python3
"""
S1 Mobile – Backend für die Handy-App
  * Slicer-Profile: Import vom PC (3MF-Projekt, Orca-Preset-Bundle, JSON) oder Sync-Ordner,
    Vererbung ("inherits") wird gegen die Orca-Systemprofile aufgelöst
  * Modelle: Upload, Download von Printables
  * Slicen mit der OrcaSlicer-CLI (Warteschlange, ein Job gleichzeitig)
  * Upload / Druckstart über Moonraker
Nur Python-Standardbibliothek. Lauscht standardmäßig auf 127.0.0.1:8090 (nginx davor).
"""
import glob, http.server, json, os, re, shutil, socketserver, subprocess, sys, threading, time
import traceback, urllib.error, urllib.parse, urllib.request, uuid, zipfile, queue

E = os.environ.get
DATA = E("S1M_DATA", "/var/lib/s1-mobile")
HOST = E("S1M_HOST", "127.0.0.1")
PORT = int(E("S1M_PORT", "8090"))
MOONRAKER = E("S1M_MOONRAKER", "http://127.0.0.1:7125").rstrip("/")
ORCA = E("S1M_ORCA", "/opt/orcaslicer/AppRun")
XVFB = E("S1M_XVFB", "auto")                 # auto | 1 | 0
SYS_PROFILES = E("S1M_SYSPROFILES", "")      # leer = automatisch im Orca-Verzeichnis suchen
SLICE_TIMEOUT = int(E("S1M_TIMEOUT", "1800"))
UPLOAD_DIR = E("S1M_UPLOAD_DIR", "S1 Mobile")  # Unterordner in Moonraker/gcodes ("" = Hauptordner)
MAX_UPLOAD = 1024 * 1024 * 1024
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp", ".gcode")

D_MODELS = os.path.join(DATA, "models")
D_IMPORT = os.path.join(DATA, "profiles", "import")
D_SYNC = E("S1M_SYNC", os.path.join(DATA, "profiles", "sync"))
D_JOBS = os.path.join(DATA, "jobs")
for _d in (D_MODELS, D_IMPORT, D_SYNC, D_JOBS):
    os.makedirs(_d, exist_ok=True)

META_KEYS = {"name", "inherits", "from", "type", "instantiation", "setting_id", "version", "is_custom_defined",
             "compatible_printers", "compatible_printers_condition", "compatible_prints",
             "compatible_prints_condition", "print_settings_id", "printer_settings_id", "filament_settings_id",
             "inherits_group", "different_settings_to_system", "print_compatible_printers",
             "upward_compatible_machine", "filament_ids", "renamed_from", "base_id", "user_id", "updated_time",
             "filament_id"}
TYPE_ALIASES = {"machine": "machine", "printer": "machine", "process": "process", "print": "process",
                "filament": "filament"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def readjson(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_name(s, maxlen=120):
    s = re.sub(r"[^\w\-. ()@+äöüÄÖÜß]", "_", str(s)).strip(" ._")
    return (s or "datei")[:maxlen]


# ============================================================ Profile
class Profiles:
    def __init__(self):
        self.lock = threading.RLock()
        self._sysdir = None
        self.sys = None          # name -> [(vendor, data)]
        self.keyclass = None     # type -> set(keys)
        self.user = {}           # name -> {"type", "data", "src"}
        self._sig = None

    # ---- Systemprofile aus der Orca-Installation
    def sysdir(self):
        if self._sysdir is None:
            cands = [SYS_PROFILES] if SYS_PROFILES else []
            base = os.path.dirname(os.path.realpath(ORCA)) if ORCA else ""
            for root in filter(None, [base, os.path.dirname(base), "/opt/orcaslicer"]):
                cands += glob.glob(os.path.join(root, "resources", "profiles"))
                cands += glob.glob(os.path.join(root, "*", "resources", "profiles"))
                cands += glob.glob(os.path.join(root, "usr", "share", "*", "resources", "profiles"))
            self._sysdir = next((c for c in cands if c and os.path.isdir(c)), "")
            if not self._sysdir and base and os.path.isdir(base):
                hit = glob.glob(os.path.join(base, "**", "profiles", "Creality.json"), recursive=True)
                self._sysdir = os.path.dirname(hit[0]) if hit else ""
        return self._sysdir

    def load_system(self):
        if self.sys is not None:
            return
        self.sys, kc = {}, {"machine": set(), "process": set(), "filament": set()}
        d = self.sysdir()
        if d:
            for p in glob.glob(os.path.join(d, "*", "**", "*.json"), recursive=True):
                j = readjson(p)
                if not isinstance(j, dict) or "name" not in j:
                    continue
                vendor = os.path.relpath(p, d).split(os.sep)[0]
                t = TYPE_ALIASES.get(str(j.get("type", "")).lower())
                if not t:
                    continue
                self.sys.setdefault(j["name"], []).append((vendor, j))
                kc[t].update(k for k in j if k not in META_KEYS)
        self.keyclass = kc
        log("Systemprofile:", d or "nicht gefunden", "–", len(self.sys), "Einträge")

    # ---- Benutzerprofile (Import + Sync-Ordner)
    def _signature(self):
        sig = []
        for root in (D_IMPORT, D_SYNC):
            for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
                try:
                    sig.append((p, os.path.getmtime(p)))
                except OSError:
                    pass
        return tuple(sorted(sig))

    def refresh(self):
        with self.lock:
            self.load_system()
            sig = self._signature()
            if sig == self._sig:
                return
            self._sig, self.user = sig, {}
            for root, src in ((D_SYNC, "sync"), (D_IMPORT, "import")):
                for p, _ in sig:
                    if not p.startswith(root + os.sep):
                        continue
                    rel = os.path.relpath(p, root).split(os.sep)
                    if "system" in rel[:-1]:
                        continue          # mitsynchronisierte Systemkopie von Orca ignorieren
                    j = readjson(p)
                    if not isinstance(j, dict):
                        continue
                    if str(j.get("from", "")).lower() == "system":
                        continue
                    t = TYPE_ALIASES.get(str(j.get("type", "")).lower())
                    if not t:
                        t = next((TYPE_ALIASES[x] for x in reversed(rel[:-1]) if x in TYPE_ALIASES), None)
                    name = j.get("name") or os.path.splitext(rel[-1])[0]
                    if not t:
                        continue
                    j = dict(j, name=name, type=t)
                    self.user[name] = {"type": t, "data": j, "src": src, "path": p}

    # ---- Vererbung auflösen
    def _resolve(self, name, vendor=None, depth=0, prefer_user=True):
        if depth > 20:
            raise ValueError("Zu tiefe Vererbung bei " + name)
        if prefer_user and name in self.user:
            d = self.user[name]["data"]
            par = d.get("inherits") or ""
            base = self._resolve(par, None, depth + 1, prefer_user=(par != name)) if par else {}
            return dict(base, **d), None
        ents = self.sys.get(name) or []
        if not ents:
            raise KeyError(name)
        ent = next((e for e in ents if e[0] == vendor), None) or ents[0]
        v, d = ent
        par = d.get("inherits") or ""
        base = self._resolve(par, v, depth + 1, prefer_user=False)[0] if par else {}
        return dict(base, **d), v

    def flatten(self, name):
        self.refresh()
        try:
            d, _ = self._resolve(name)
        except KeyError as e:
            raise ValueError(f"Profil nicht gefunden: {e.args[0]}")
        d = dict(d)
        d.update({"name": name, "inherits": "", "from": "User", "instantiation": "true",
                  "compatible_printers": [], "compatible_printers_condition": "",
                  "compatible_prints": [], "compatible_prints_condition": ""})
        for k in ("setting_id", "base_id", "user_id", "updated_time", "is_custom_defined"):
            d.pop(k, None)
        return d

    def system_base(self, name):
        """Name des ersten System-Drucker-Profils in der Vererbungskette (für Kompatibilität)."""
        seen = 0
        while name and seen < 20:
            seen += 1
            if name in self.user:
                name = self.user[name]["data"].get("inherits") or ""
                continue
            if name in self.sys:
                return name
            return None
        return None

    def listing(self, machine=None):
        self.refresh()
        out = {"machine": [], "process": [], "filament": []}
        for n, u in sorted(self.user.items()):
            out[u["type"]].append({"name": n, "src": u["src"]})
        sys_m = []
        for n, ents in self.sys.items():
            for v, d in ents:
                if d.get("type") == "machine" and str(d.get("instantiation")) == "true" and re.search(r"Ender-3 S1\b", n):
                    sys_m.append(n)
        for n in sorted(set(sys_m)):
            if n not in self.user:
                out["machine"].append({"name": n, "src": "system"})
        if not machine:
            machine = self.default_machine(out)
        base = self.system_base(machine) if machine else None
        if machine and not base:
            # z.B. aus 3MF importiertes, vollständig aufgelöstes Profil → passendes Systemprofil raten
            try:
                md0 = self.flatten(machine)
            except Exception:
                md0 = {}
            pm, nz = md0.get("printer_model"), (md0.get("nozzle_diameter") or [""])[0]
            for n, ents in self.sys.items():
                d = ents[0][1]
                if d.get("type") == "machine" and str(d.get("instantiation")) == "true" and pm and \
                        d.get("printer_model") == pm and str(nz) in n:
                    base = n
                    break
            base = base or ("Creality Ender-3 S1 0.4 nozzle" if "Creality Ender-3 S1 0.4 nozzle" in self.sys else None)
        if base:
            for n, ents in sorted(self.sys.items()):
                for v, d in ents:
                    t = TYPE_ALIASES.get(str(d.get("type", "")).lower())
                    if t in ("process", "filament") and str(d.get("instantiation")) == "true" \
                            and base in (d.get("compatible_printers") or []) and n not in self.user:
                        out[t].append({"name": n, "src": "system"})
                        break
        dflt = {"machine": machine, "process": None, "filament": None}
        try:
            md = self.flatten(machine) if machine else {}
        except Exception:
            md = {}
        names = {t: [x["name"] for x in out[t]] for t in out}
        u_proc = [x["name"] for x in out["process"] if x["src"] != "system"]
        u_fil = [x["name"] for x in out["filament"] if x["src"] != "system"]
        dp = md.get("default_print_profile")
        dflt["process"] = (u_proc[0] if u_proc else None) or (dp if dp in names["process"] else None) or (names["process"] or [None])[0]
        df = (md.get("default_filament_profile") or [None])
        df = df[0] if isinstance(df, list) and df else df
        dflt["filament"] = (u_fil[0] if u_fil else None) or (df if df in names["filament"] else None) or (names["filament"] or [None])[0]
        return {"profiles": out, "default": dflt, "sysdir": bool(self.sysdir()),
                "counts": {t: sum(1 for x in out[t] if x["src"] != "system") for t in out}}

    def default_machine(self, out):
        um = [x["name"] for x in out["machine"] if x["src"] != "system"]
        pick = next((n for n in um if re.search(r"S1", n)), None) or (um[0] if um else None)
        if pick:
            return pick
        sm = [x["name"] for x in out["machine"]]
        return next((n for n in sm if re.search(r"Ender-3 S1 0\.4", n)), None) or (sm[0] if sm else None)

    # ---- Import vom PC
    def import_file(self, filename, path):
        self.load_system()
        low = filename.lower()
        added = []
        if low.endswith(".3mf"):
            with zipfile.ZipFile(path) as z:
                nm = next((n for n in z.namelist() if n.lower().endswith("metadata/project_settings.config")), None)
                if not nm:
                    raise ValueError("Diese 3MF enthält keine Orca/Bambu-Projekteinstellungen "
                                     "(in OrcaSlicer mit „Projekt speichern“ erzeugen).")
                cfg = json.loads(z.read(nm).decode("utf-8"))
            added += self._split_project(cfg, os.path.splitext(os.path.basename(filename))[0])
        elif low.endswith((".zip", ".orca_printer", ".orca_filament", ".bbscfg", ".bbsflmt")):
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if not n.lower().endswith(".json") or n.endswith("/"):
                        continue
                    try:
                        j = json.loads(z.read(n).decode("utf-8"))
                    except Exception:
                        continue
                    if not isinstance(j, dict) or "name" not in j:
                        continue
                    parts = n.replace("\\", "/").split("/")
                    t = TYPE_ALIASES.get(str(j.get("type", "")).lower()) or \
                        next((TYPE_ALIASES[x.lower()] for x in reversed(parts[:-1]) if x.lower() in TYPE_ALIASES), None)
                    if not t:
                        continue
                    added.append(self._save(t, dict(j, type=t)))
        elif low.endswith(".json"):
            j = readjson(path)
            if not isinstance(j, dict) or "name" not in j:
                raise ValueError("JSON ist kein Orca-Profil (Feld „name“ fehlt).")
            t = TYPE_ALIASES.get(str(j.get("type", "")).lower())
            if not t:
                raise ValueError("Profiltyp unbekannt – bitte als Bundle oder 3MF exportieren.")
            added.append(self._save(t, dict(j, type=t)))
        else:
            raise ValueError("Unterstützt: .3mf (Projekt), .orca_printer/.zip (Preset-Bundle), .json")
        if not added:
            raise ValueError("Keine Profile in der Datei gefunden.")
        self._sig = None
        return added

    def _split_project(self, cfg, label):
        kc = self.keyclass or {}
        mach, proc, fil = {}, {}, {}
        for k, v in cfg.items():
            if k in META_KEYS:
                continue
            if k in kc.get("filament", ()):
                fil[k] = v
            elif k in kc.get("machine", ()):
                mach[k] = v
            else:
                proc[k] = v
        sfx = " · PC"
        mname = (cfg.get("printer_settings_id") or "Drucker " + label) + sfx
        pname = (cfg.get("print_settings_id") or "Druck " + label) + sfx
        out = [self._save("machine", dict(mach, name=mname, type="machine", inherits="", **{"from": "User"})),
               self._save("process", dict(proc, name=pname, type="process", inherits="", **{"from": "User"}))]
        fids = cfg.get("filament_settings_id") or ["Filament " + label]
        if not isinstance(fids, list):
            fids = [fids]
        seen = set()
        for i, fid in enumerate(fids):
            if fid in seen:
                continue
            seen.add(fid)
            fd = {}
            for k, v in fil.items():
                if isinstance(v, list):
                    fd[k] = [v[i] if i < len(v) else (v[0] if v else "")]
                else:
                    fd[k] = v
            out.append(self._save("filament", dict(fd, name=fid + sfx, type="filament", inherits="", **{"from": "User"})))
        return out

    def _save(self, t, j):
        d = os.path.join(D_IMPORT, t)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, safe_name(j["name"]) + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=1)
        return {"type": t, "name": j["name"]}

    def clear_imports(self):
        shutil.rmtree(D_IMPORT, ignore_errors=True)
        os.makedirs(D_IMPORT, exist_ok=True)
        self._sig = None


PROFILES = Profiles()


# ============================================================ Modelle
def model_list():
    out = []
    for d in glob.glob(os.path.join(D_MODELS, "*", "meta.json")):
        m = readjson(d)
        if m and os.path.exists(os.path.join(os.path.dirname(d), m.get("file", ""))):
            out.append(m)
    return sorted(out, key=lambda m: -m.get("added", 0))


def model_get(mid):
    if not re.fullmatch(r"[0-9a-f]{6,32}", mid or ""):
        raise ValueError("Ungültige Modell-ID")
    m = readjson(os.path.join(D_MODELS, mid, "meta.json"))
    if not m:
        raise KeyError("Modell nicht gefunden")
    m["path"] = os.path.join(D_MODELS, mid, m["file"])
    return m


def model_add(filename, src_path, source="upload", extra=None):
    filename = safe_name(os.path.basename(filename))
    low = filename.lower()
    if low.endswith(".zip"):
        return model_add_zip(filename, src_path, source, extra)
    if not low.endswith(MODEL_EXT):
        raise ValueError("Format nicht unterstützt (STL, 3MF, OBJ, STEP, G-Code)")
    mid = uuid.uuid4().hex[:12]
    d = os.path.join(D_MODELS, mid)
    os.makedirs(d)
    shutil.move(src_path, os.path.join(d, filename))
    m = {"id": mid, "name": os.path.splitext(filename)[0], "file": filename, "ext": os.path.splitext(low)[1][1:],
         "size": os.path.getsize(os.path.join(d, filename)), "added": time.time(), "source": source}
    if extra:
        m.update(extra)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return m


def model_add_zip(filename, path, source, extra):
    added = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.lower().endswith(MODEL_EXT) and not n.endswith("/") and "__MACOSX" not in n:
                tmp = os.path.join(DATA, "tmp-" + uuid.uuid4().hex)
                with z.open(n) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                added.append(model_add(os.path.basename(n), tmp, source, extra))
    os.remove(path)
    if not added:
        raise ValueError("Im ZIP wurden keine Modelle gefunden")
    return added[0] if len(added) == 1 else {"multi": added}


# ============================================================ Printables (inoffizielle GraphQL-API)
GQL = "https://api.printables.com/graphql/"


def gql(op, query, variables):
    body = json.dumps({"operationName": op, "query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL, data=body, headers={"Content-Type": "application/json", "User-Agent": UA,
                                                        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    if data.get("errors"):
        raise RuntimeError("Printables: " + "; ".join(e.get("message", "?") for e in data["errors"])[:300])
    return data.get("data") or {}


MODEL_FRAG = """fragment Model on PrintType { id name slug ratingAvg likesCount downloadCount datePublished
  user { id handle publicUsername __typename } image { filePath __typename } __typename }"""


def img_url(fp):
    return ("https://media.printables.com/" + fp) if fp else None


def pr_item(m):
    return {"id": m.get("id"), "name": m.get("name"), "slug": m.get("slug"),
            "likes": m.get("likesCount"), "downloads": m.get("downloadCount"), "rating": m.get("ratingAvg"),
            "author": (m.get("user") or {}).get("publicUsername"), "image": img_url((m.get("image") or {}).get("filePath")),
            "url": f"https://www.printables.com/model/{m.get('id')}-{m.get('slug')}"}


def printables_search(q, ordering="best_match", limit=24, offset=0):
    if ordering not in ("best_match", "popular", "latest", "rating", "makes_count"):
        ordering = "best_match"
    q1 = """query SearchModels($query: String!, $limit: Int, $offset: Int, $ordering: SearchChoicesEnum) {
      result: searchPrints2(query: $query, printType: print, limit: $limit, offset: $offset, ordering: $ordering) {
        items { ...Model __typename } __typename } } """ + MODEL_FRAG
    q2 = """query SearchModels($query: String!, $limit: Int, $ordering: SearchChoicesEnum) {
      result: searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) {
        items { ...Model __typename } __typename } } """ + MODEL_FRAG
    try:
        d = gql("SearchModels", q1, {"query": q, "limit": limit, "offset": offset, "ordering": ordering})
    except RuntimeError:
        if offset:
            return []   # Paging nicht unterstützt
        d = gql("SearchModels", q2, {"query": q, "limit": limit, "ordering": ordering})
    return [pr_item(m) for m in ((d.get("result") or {}).get("items") or [])]


FILES_Q = """query ModelFiles($id: ID!) { model: print(id: $id) { id filesType
  gcodes { id name fileSize filePreviewPath __typename }
  stls { id name fileSize filePreviewPath __typename }
  otherFiles { id name fileSize filePreviewPath __typename } __typename } }"""
DETAIL_Q = """query ModelDetail($id: ID!) { model: print(id: $id) { id name slug summary likesCount downloadCount ratingAvg
  user { publicUsername __typename } image { filePath __typename } images { filePath __typename } __typename } }"""
DETAIL_Q_MIN = """query ModelDetail($id: ID!) { model: print(id: $id) { id name slug likesCount downloadCount
  user { publicUsername __typename } image { filePath __typename } __typename } }"""


def printables_model(mid):
    if not re.fullmatch(r"\d{1,12}", str(mid)):
        raise ValueError("Ungültige Printables-ID")
    files = (gql("ModelFiles", FILES_Q, {"id": mid}).get("model") or {})
    try:
        det = gql("ModelDetail", DETAIL_Q, {"id": mid}).get("model") or {}
    except Exception:
        try:
            det = gql("ModelDetail", DETAIL_Q_MIN, {"id": mid}).get("model") or {}
        except Exception:
            det = {}
    out = pr_item(det) if det else {"id": mid}
    out["summary"] = det.get("summary")
    imgs = [img_url(i.get("filePath")) for i in (det.get("images") or []) if i.get("filePath")]
    out["images"] = imgs or ([out["image"]] if out.get("image") else [])
    fl = []
    for key, ftype in (("stls", "stl"), ("gcodes", "gcode"), ("otherFiles", "other")):
        for f in files.get(key) or []:
            fl.append({"id": f.get("id"), "name": f.get("name"), "size": f.get("fileSize"), "type": ftype,
                       "preview": img_url(f.get("filePreviewPath"))})
    out["files"] = fl
    return out


def printables_download(model_id, file_id, ftype, name, meta):
    q = """mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {
      getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
        ok errors { field messages __typename } output { link count ttl __typename } __typename } }"""
    d = gql("GetDownloadLink", q, {"id": str(file_id), "modelId": str(model_id), "fileType": ftype, "source": "model_detail"})
    r = d.get("getDownloadLink") or {}
    link = (r.get("output") or {}).get("link")
    if not r.get("ok") or not link:
        raise RuntimeError("Printables hat keinen Download-Link geliefert: " + json.dumps(r.get("errors"))[:200])
    tmp = os.path.join(DATA, "tmp-" + uuid.uuid4().hex)
    req = urllib.request.Request(link, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    fname = name or os.path.basename(urllib.parse.urlparse(link).path) or "modell.stl"
    extra = {"printables": {"id": str(model_id), "url": meta.get("url"), "image": meta.get("image"),
                            "author": meta.get("author"), "title": meta.get("title")}}
    if meta.get("image"):
        extra["image"] = meta.get("image")
    return model_add(fname, tmp, "printables", extra)


# ============================================================ Slicen
JOBS = {}
JQ = queue.Queue()

PROC_KEYS = {
    "layer_height": lambda v: str(float(v)),
    "sparse_infill_density": lambda v: f"{int(float(v))}%",
    "wall_loops": lambda v: str(int(v)),
    "brim_type": lambda v: {"none": "no_brim", "auto": "auto_brim", "outer": "outer_only"}.get(v, v),
}
SUPPORT = {"none": {"enable_support": "0"},
           "normal": {"enable_support": "1", "support_type": "normal(auto)"},
           "tree": {"enable_support": "1", "support_type": "tree(auto)"}}


def apply_overrides(proc, fil, ov):
    ov = ov or {}
    s = ov.get("support")
    if s in SUPPORT:
        proc.update(SUPPORT[s])
    if "support_on_build_plate_only" in ov and s in ("normal", "tree"):
        proc["support_on_build_plate_only"] = "1" if ov["support_on_build_plate_only"] else "0"
    for k, f in PROC_KEYS.items():
        if ov.get(k) not in (None, ""):
            proc[k] = f(ov[k])
    if ov.get("nozzle_temp"):
        t = str(int(ov["nozzle_temp"]))
        fil["nozzle_temperature"] = [t]
        fil["nozzle_temperature_initial_layer"] = [t]
    if ov.get("bed_temp"):
        t = str(int(ov["bed_temp"]))
        for k in list(fil):
            if k.endswith("_plate_temp") or k.endswith("_plate_temp_initial_layer"):
                fil[k] = [t]


def parse_gcode_stats(path):
    st = {}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(200_000)
            f.seek(max(0, size - 200_000))
            tail = f.read()
        txt = (head + b"\n" + tail).decode("utf-8", "ignore")
        pats = {
            "time": [r";\s*estimated printing time \(normal mode\)\s*=\s*([^\n]+)", r";\s*total estimated time:\s*([^\n;]+)",
                     r";\s*model printing time:\s*([^\n;]+)"],
            "weight": [r";\s*total filament used \[g\]\s*=\s*([\d.]+)", r";\s*filament used \[g\]\s*=\s*([\d.]+)"],
            "length": [r";\s*total filament length \[mm\]\s*:\s*([\d.]+)", r";\s*filament used \[mm\]\s*=\s*([\d.]+)"],
            "layers": [r";\s*total layer number:\s*(\d+)", r";\s*total layers count\s*=\s*(\d+)"],
        }
        for k, ps in pats.items():
            for p in ps:
                m = re.search(p, txt)
                if m:
                    st[k] = m.group(1).strip()
                    break
        st["thumbnail"] = "; thumbnail begin" in txt or "; THUMBNAIL_BLOCK_START" in txt
    except Exception:
        pass
    return st


def use_xvfb():
    if XVFB == "1":
        return True
    if XVFB == "0":
        return False
    return bool(shutil.which("xvfb-run")) and not os.environ.get("DISPLAY")


def run_job(job):
    req = job["req"]
    jd = job["dir"]
    m = model_get(req["model"])
    job.update(state="running", started=time.time(), progress="Profile vorbereiten")
    mach = PROFILES.flatten(req["machine"])
    proc = PROFILES.flatten(req["process"])
    fil = PROFILES.flatten(req["filament"])
    apply_overrides(proc, fil, req.get("overrides"))
    # Die Orca-CLI prüft, ob Prozess/Filament zum Drucker passen: sie vergleicht compatible_printers mit
    # dem "Systemnamen" des Druckers (bei from=system = sein eigener Name). Die Profile sind vollständig
    # aufgelöst, deshalb als "system" markieren und explizit miteinander verknüpfen (sonst Fehler -17).
    for d in (mach, proc, fil):
        d["from"] = "system"
        d["inherits"] = ""
    proc["compatible_printers"] = [mach["name"]]
    fil["compatible_printers"] = [mach["name"]]
    files = {}
    for n, d in (("machine", mach), ("process", proc), ("filament", fil)):
        files[n] = os.path.join(jd, n + ".json")
        with open(files[n], "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    out = os.path.join(jd, "out")
    os.makedirs(out, exist_ok=True)
    ov = req.get("overrides") or {}
    cmd = [ORCA, "--load-settings", f"{files['machine']};{files['process']}", "--load-filaments", files["filament"],
           "--arrange", "1" if ov.get("arrange", True) else "0",
           "--orient", "1" if ov.get("orient") else "0"]
    sc = float(ov.get("scale") or 100)
    if abs(sc - 100) > 0.01:
        cmd += ["--scale", f"{sc / 100:.4f}"]
    cmd += ["--slice", "0", "--outputdir", out, m["path"]]
    if use_xvfb():
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24"] + cmd
    job["cmd"] = " ".join(cmd)
    job["progress"] = "OrcaSlicer läuft"
    logp = os.path.join(jd, "slicer.log")
    env = dict(os.environ, HOME=DATA, LC_ALL="C.UTF-8")
    with open(logp, "wb") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=jd, env=env)
        job["pid"] = p.pid
        try:
            rc = p.wait(timeout=SLICE_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            raise RuntimeError("Zeitüberschreitung beim Slicen")
    gcodes = sorted(glob.glob(os.path.join(out, "**", "*.gcode"), recursive=True))
    if not gcodes:
        for tm in glob.glob(os.path.join(out, "**", "*.3mf"), recursive=True):
            with zipfile.ZipFile(tm) as z:
                for n in z.namelist():
                    if re.search(r"plate_\d+\.gcode$", n):
                        dst = os.path.join(out, os.path.basename(n))
                        with z.open(n) as s, open(dst, "wb") as d:
                            shutil.copyfileobj(s, d)
                        gcodes.append(dst)
    if rc != 0 or not gcodes:
        tail = tail_file(logp, 2500)
        code = rc - 256 if rc > 127 else rc
        hint = CLI_ERR.get(code, "")
        raise RuntimeError(f"OrcaSlicer ist fehlgeschlagen (Code {code})" + (f": {hint}" if hint else ".") + f"\n{tail}")
    base = safe_name(m["name"], 60)
    tag = re.sub(r"\s*·\s*PC$", "", req["process"])
    tag = safe_name(re.sub(r"@.*$", "", tag).strip(), 30)
    results = []
    for i, g in enumerate(gcodes):
        plate = re.search(r"plate_(\d+)", os.path.basename(g))
        pn = int(plate.group(1)) if plate else i + 1
        fname = f"{base}_{tag}" + (f"_P{pn}" if len(gcodes) > 1 else "") + ".gcode"
        fname = fname.replace(" ", "_")
        dst = os.path.join(jd, fname)
        shutil.move(g, dst)
        results.append(dict(parse_gcode_stats(dst), file=fname, plate=pn, size=os.path.getsize(dst)))
    job.update(state="done", results=results, finished=time.time(), progress="Fertig")


CLI_ERR = {
    -1: "Slicer-Umgebung konnte nicht gestartet werden",
    -2: "Ungültige Parameter",
    -3: "Modell- oder Profildatei nicht gefunden",
    -5: "Profildatei ungültig – Profil neu vom PC importieren",
    -6: "Modelldatei kann nicht gelesen werden (defekt oder falsches Format)",
    -14: "Nicht genug Arbeitsspeicher – dem Container mehr RAM geben (pct set <ID> --memory 4096)",
    -17: "Prozessprofil passt nicht zum Drucker",
    -21: "Objekte passen nicht auf das Druckbett (Größe verringern)",
    -22: "Automatisches Ausrichten fehlgeschlagen – „Automatisch ausrichten“ ausschalten",
    -50: "Kein druckbares Objekt auf dem Bett (zu groß oder außerhalb)",
    -51: "Einstellungen ungültig (z. B. Schichthöhe größer als Düse erlaubt)",
    -52: "Objekt ragt über das Druckbett hinaus – Größe verringern oder „Auf Platte anordnen“ einschalten",
    -58: "Slicen dauert zu lange (Zeitlimit)",
    -59: "Modell hat zu viele Dreiecke",
    -61: "Filament passt nicht zur eingestellten Druckplatte",
    -100: "Fehler beim Slicen (Modell prüfen/reparieren)",
    -101: "Druckwege kollidieren",
    -102: "Druckwege liegen außerhalb des druckbaren Bereichs",
}


def tail_file(p, n):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            s = f.tell()
            f.seek(max(0, s - n))
            return f.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def worker():
    while True:
        job = JQ.get()
        try:
            run_job(job)
        except Exception as e:
            log("Job", job["id"], "Fehler:", e)
            job.update(state="error", error=str(e), finished=time.time())
        finally:
            job.pop("pid", None)
            with open(os.path.join(job["dir"], "job.json"), "w") as f:
                json.dump({k: v for k, v in job.items() if k != "dir"}, f)


def job_public(j):
    d = {k: v for k, v in j.items() if k not in ("dir", "pid")}
    if j["state"] in ("running", "error"):
        d["log"] = tail_file(os.path.join(j["dir"], "slicer.log"), 1500)
    if j["state"] == "queued":
        d["position"] = sum(1 for x in JOBS.values() if x["state"] == "queued" and x["created"] <= j["created"])
    return d


def load_old_jobs():
    for p in glob.glob(os.path.join(D_JOBS, "*", "job.json")):
        j = readjson(p)
        if j and j.get("state") in ("done", "error"):
            j["dir"] = os.path.dirname(p)
            JOBS[j["id"]] = j
    # alte Jobs (> 7 Tage) aufräumen
    for jid, j in list(JOBS.items()):
        if time.time() - j.get("created", 0) > 7 * 86400:
            shutil.rmtree(j["dir"], ignore_errors=True)
            JOBS.pop(jid, None)


# ============================================================ Moonraker
def moonraker_upload(path, filename, start):
    boundary = "----s1m" + uuid.uuid4().hex
    fields = {"root": "gcodes", "print": "true" if start else "false"}
    if UPLOAD_DIR:
        fields["path"] = UPLOAD_DIR
    pre = b""
    for k, v in fields.items():
        pre += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    pre += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    with open(path, "rb") as f:
        body = pre + f.read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(MOONRAKER + "/server/files/upload", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"Moonraker: HTTP {e.code} {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Moonraker nicht erreichbar ({MOONRAKER}): {e.reason}")


# ============================================================ Info
def orca_version():
    if not os.path.exists(ORCA):
        return None
    vf = os.path.join(DATA, "orca_version.txt")
    v = (open(vf).read().strip() if os.path.exists(vf) else "") or "installiert"
    return v


# ============================================================ HTTP
class H(http.server.BaseHTTPRequestHandler):
    server_version = "S1Mobile/1.0"

    def log_message(self, fmt, *a):
        if os.environ.get("S1M_DEBUG"):
            log(self.address_string(), fmt % a)

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def err(self, msg, code=400):
        self.send_json({"error": str(msg)}, code)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("Anfrage zu groß")
        return json.loads(self.rfile.read(n) or b"{}")

    def body_to_file(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("Leere Datei")
        if n > MAX_UPLOAD:
            raise ValueError("Datei zu groß")
        tmp = os.path.join(DATA, "tmp-" + uuid.uuid4().hex)
        left = n
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        return tmp

    def fname(self):
        return urllib.parse.unquote(self.headers.get("X-Filename") or "datei")

    def send_file(self, path, name=None, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        if name:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        path = re.sub(r"^/api", "", u.path).rstrip("/") or "/"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            # ---- Info
            if method == "GET" and path == "/info":
                return self.send_json({"orca": orca_version(), "orca_path": ORCA, "moonraker": MOONRAKER,
                                       "sysprofiles": bool(PROFILES.sysdir()), "sync_dir": D_SYNC,
                                       "queue": sum(1 for j in JOBS.values() if j["state"] in ("queued", "running"))})
            # ---- Profile
            if method == "GET" and path == "/profiles":
                return self.send_json(PROFILES.listing(q.get("machine")))
            if method == "POST" and path == "/profiles/import":
                tmp = self.body_to_file()
                try:
                    added = PROFILES.import_file(self.fname(), tmp)
                finally:
                    os.path.exists(tmp) and os.remove(tmp)
                return self.send_json({"added": added})
            if method == "POST" and path == "/profiles/clear":
                PROFILES.clear_imports()
                return self.send_json({"ok": True})
            # ---- Modelle
            if method == "GET" and path == "/models":
                return self.send_json({"models": model_list()})
            if method == "POST" and path == "/models/upload":
                tmp = self.body_to_file()
                try:
                    return self.send_json(model_add(self.fname(), tmp, "upload"))
                finally:
                    os.path.exists(tmp) and os.remove(tmp)
            mm = re.fullmatch(r"/models/([0-9a-f]+)(/file)?", path)
            if mm and method == "GET" and mm.group(2):
                m = model_get(mm.group(1))
                return self.send_file(m["path"], m["file"])
            if mm and method == "DELETE":
                model_get(mm.group(1))
                shutil.rmtree(os.path.join(D_MODELS, mm.group(1)), ignore_errors=True)
                return self.send_json({"ok": True})
            mm = re.fullmatch(r"/models/([0-9a-f]+)/send", path)
            if mm and method == "POST":           # G-Code-Datei direkt an Drucker
                m = model_get(mm.group(1))
                if m["ext"] != "gcode":
                    raise ValueError("Nur G-Code kann direkt gesendet werden")
                b = self.body_json()
                return self.send_json(moonraker_upload(m["path"], m["file"], bool(b.get("print"))))
            # ---- Slicen
            if method == "POST" and path == "/slice":
                b = self.body_json()
                for k in ("model", "machine", "process", "filament"):
                    if not b.get(k):
                        raise ValueError(f"„{k}“ fehlt")
                m = model_get(b["model"])
                if m["ext"] == "gcode":
                    raise ValueError("G-Code muss nicht gesliced werden")
                if not os.path.exists(ORCA):
                    raise RuntimeError("OrcaSlicer ist nicht installiert (install-mobile.sh ausführen)")
                jid = uuid.uuid4().hex[:12]
                jd = os.path.join(D_JOBS, jid)
                os.makedirs(jd)
                job = {"id": jid, "dir": jd, "req": b, "state": "queued", "created": time.time(),
                       "model_name": m["name"], "progress": "In Warteschlange"}
                JOBS[jid] = job
                JQ.put(job)
                return self.send_json(job_public(job))
            if method == "GET" and path == "/jobs":
                js = sorted(JOBS.values(), key=lambda j: -j["created"])[:30]
                return self.send_json({"jobs": [job_public(j) for j in js]})
            mm = re.fullmatch(r"/jobs/([0-9a-f]+)(/send|/gcode/(\d+))?", path)
            if mm:
                j = JOBS.get(mm.group(1))
                if not j:
                    return self.err("Job nicht gefunden", 404)
                if method == "GET" and not mm.group(2):
                    return self.send_json(job_public(j))
                if method == "GET" and mm.group(3):
                    r = (j.get("results") or [])[int(mm.group(3))]
                    return self.send_file(os.path.join(j["dir"], r["file"]), r["file"], "text/plain")
                if method == "POST" and mm.group(2) == "/send":
                    b = self.body_json()
                    if j["state"] != "done":
                        raise ValueError("Job ist noch nicht fertig")
                    r = (j.get("results") or [])[int(b.get("index", 0))]
                    res = moonraker_upload(os.path.join(j["dir"], r["file"]), r["file"], bool(b.get("print")))
                    return self.send_json({"ok": True, "moonraker": res,
                                           "path": ((UPLOAD_DIR + "/") if UPLOAD_DIR else "") + r["file"]})
            # ---- Printables
            if method == "GET" and path == "/printables/search":
                return self.send_json({"items": printables_search(q.get("q", ""), q.get("ordering", "best_match"),
                                                                  min(int(q.get("limit", 24)), 48), int(q.get("offset", 0)))})
            mm = re.fullmatch(r"/printables/model/(\d+)", path)
            if mm and method == "GET":
                return self.send_json(printables_model(mm.group(1)))
            if method == "POST" and path == "/printables/download":
                b = self.body_json()
                r = printables_download(b["modelId"], b["fileId"], b.get("fileType", "stl"), b.get("name"),
                                        b.get("meta") or {})
                return self.send_json(r)
            return self.err("Unbekannter Endpunkt", 404)
        except (ValueError, KeyError) as e:
            return self.err(e.args[0] if e.args else e, 400)
        except urllib.error.URLError as e:
            return self.err(f"Netzwerkfehler: {getattr(e, 'reason', e)}", 502)
        except Exception as e:
            traceback.print_exc()
            return self.err(e, 500)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    for p in glob.glob(os.path.join(DATA, "tmp-*")):
        try:
            os.remove(p)
        except OSError:
            pass
    load_old_jobs()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=PROFILES.refresh, daemon=True).start()
    log(f"S1 Mobile Backend auf http://{HOST}:{PORT} – Orca: {ORCA} – Moonraker: {MOONRAKER}")
    Server((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
