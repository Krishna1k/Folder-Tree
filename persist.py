import os, re, csv, json, time, shutil, struct, zipfile, hashlib
import threading, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.modalview import ModalView
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import (MDFillRoundFlatButton, MDFlatButton,
                               MDRaisedButton)
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDLabel
from kivymd.uix.list import MDList, OneLineListItem
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.screen import MDScreen
from kivymd.uix.screenmanager import MDScreenManager
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.textfield import MDTextField
from kivymd.uix.toolbar import MDTopAppBar

ROOT = Path("/storage/emulated/0")
PIN = "\U0001F4CC"
DATE_FMT = "%d-%b-%Y %H:%M:%S"
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
             ".bmp", ".dng", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".3gp", ".webm", ".m4v",
             ".mpg", ".mpeg"}
CARD_BG = (0.15, 0.15, 0.2, 1)


# ---------------------------------------------------------------- helpers
def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime(DATE_FMT)
    except Exception:
        return ""


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip() or "Photos"


def guess_root(stem):
    m = re.match(r"(?:Persist|manifest)_(.+)_\d{8}_\d{6}$", stem)
    return m.group(1) if m else stem


def safe(base, rel):
    t = (base / rel).resolve()
    return t if base.resolve() in t.parents else None


def list_files(root):
    out = []
    for d, _, names in os.walk(root):
        for n in names:
            out.append(Path(d) / n)
    return out


def disp(name):
    return name.replace(PIN, "[PIN]")


def _tiff_date(t):
    bo = "<" if t[:2] == b"II" else ">"

    def u16(o):
        return struct.unpack(bo + "H", t[o:o + 2])[0]

    def u32(o):
        return struct.unpack(bo + "I", t[o:o + 4])[0]

    def ifd(off):
        out = {}
        for k in range(u16(off)):
            e = off + 2 + 12 * k
            out[u16(e)] = (u16(e + 2), u32(e + 4), e + 8)
        return out

    def text(ent):
        _, cnt, pos = ent
        off = pos if cnt <= 4 else u32(pos)
        return t[off:off + cnt].split(b"\x00")[0].decode(
            "ascii", "ignore").strip()

    i0 = ifd(u32(4))
    val = ""
    if 0x8769 in i0:
        ex = ifd(u32(i0[0x8769][2]))
        for tag in (0x9003, 0x9004):
            if tag in ex:
                val = text(ex[tag])
                break
    if not val and 0x0132 in i0:
        val = text(i0[0x0132])
    if val:
        return datetime.strptime(val, "%Y:%m:%d %H:%M:%S").timestamp()
    return None


def exif_taken(path):
    try:
        with open(path, "rb") as f:
            head = f.read(262144)
        if head[:2] != b"\xff\xd8":
            return None
        i = 2
        while i + 4 <= len(head) and head[i] == 0xFF:
            mk = head[i + 1]
            if mk == 0xDA:
                break
            ln = struct.unpack(">H", head[i + 2:i + 4])[0]
            if mk == 0xE1 and head[i + 4:i + 10] == b"Exif\x00\x00":
                return _tiff_date(head[i + 10:i + 2 + ln])
            i += 2 + ln
    except Exception:
        pass
    return None


# ------------------------------------------------------- android helpers
def _act():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


def ask_access():
    try:
        from android.permissions import request_permissions
        request_permissions([
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.POST_NOTIFICATIONS"])
    except Exception:
        pass
    try:
        from jnius import autoclass
        if autoclass("android.os.Environment").isExternalStorageManager():
            return
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        act = _act()
        i = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        i.setData(Uri.parse("package:" + act.getPackageName()))
        act.startActivity(i)
    except Exception:
        pass


def open_app_settings():
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        act = _act()
        i = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
        i.setData(Uri.parse("package:" + act.getPackageName()))
        act.startActivity(i)
    except Exception:
        pass


def request_battery_exemption():
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        Context = autoclass("android.content.Context")
        act = _act()
        pkg = act.getPackageName()
        pm = act.getSystemService(Context.POWER_SERVICE)
        if pm.isIgnoringBatteryOptimizations(pkg):
            return "Already Unrestricted"
        i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
        i.setData(Uri.parse("package:" + pkg))
        act.startActivity(i)
        return "Asked"
    except Exception:
        return "Failed"


def notify(title, text):
    try:
        from jnius import autoclass, cast
        String = autoclass("java.lang.String")
        Context = autoclass("android.content.Context")
        NM = autoclass("android.app.NotificationManager")
        NC = autoclass("android.app.NotificationChannel")
        Builder = autoclass("android.app.Notification$Builder")
        Intent = autoclass("android.content.Intent")
        PI = autoclass("android.app.PendingIntent")
        act = _act()
        cs = "java.lang.CharSequence"
        nm = act.getSystemService(Context.NOTIFICATION_SERVICE)
        cid = "persist_alerts"
        nm.createNotificationChannel(
            NC(cid, cast(cs, String("Persist Alerts")), NM.IMPORTANCE_HIGH))
        b = Builder(act, cid)
        b.setContentTitle(cast(cs, String(title)))
        b.setContentText(cast(cs, String(text[:200])))
        try:
            b.setSmallIcon(act.getApplicationInfo().icon)
        except Exception:
            b.setSmallIcon(autoclass("android.R$drawable").ic_dialog_info)
        try:
            it = Intent(act, act.getClass())
            b.setContentIntent(PI.getActivity(
                act, 0, it, PI.FLAG_IMMUTABLE | PI.FLAG_UPDATE_CURRENT))
        except Exception:
            pass
        b.setAutoCancel(True)
        nm.notify(1, b.build())
    except Exception:
        pass


def vibrate(ok):
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        VE = autoclass("android.os.VibrationEffect")
        vib = _act().getSystemService(Context.VIBRATOR_SERVICE)
        for ms in ([700] if ok else [250, 250]):
            vib.vibrate(VE.createOneShot(ms, VE.DEFAULT_AMPLITUDE))
            time.sleep(ms / 1000.0 + 0.2)
    except Exception:
        pass


# ------------------------------------------------------------------ scan
def hash_one(root, p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(4 << 20), b""):
            h.update(c)
    st = p.stat()
    ext = p.suffix.lower()
    kind = ("photo" if ext in PHOTO_EXT
            else "video" if ext in VIDEO_EXT else "other")
    taken = exif_taken(p) if ext in (".jpg", ".jpeg") else None
    return {"path": p.relative_to(root).as_posix(), "name": p.name,
            "kind": kind, "size": st.st_size, "hash": h.hexdigest(),
            "mtime": st.st_mtime, "modified": fmt_ts(st.st_mtime),
            "taken_ts": taken, "taken": fmt_ts(taken) if taken else ""}


def do_scan(app, root, db_dir):
    t0 = time.time()
    app.set_status("Listing files...", 0)
    files = list_files(root)
    total = len(files)
    if not total:
        raise RuntimeError("No files found in " + root.name)
    w = app.workers()
    app.log("Scanning %d files in '%s' using %d core(s)" %
            (total, root.name, w))
    items, failed = [], []
    with ThreadPoolExecutor(max_workers=w) as ex:
        futs = {ex.submit(hash_one, root, p): p for p in files}
        for n, fu in enumerate(as_completed(futs), 1):
            try:
                items.append(fu.result())
            except Exception as e:
                failed.append("%s (%r)" % (futs[fu].name, e))
            if n % 20 == 0 or n == total:
                app.set_status("Scanning %d / %d" % (n, total),
                               n * 100.0 / total)
    items.sort(key=lambda x: x["path"])
    db_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = re.sub(r"[^A-Za-z0-9_-]", "_", root.name)
    dbp = db_dir / ("Persist_%s_%s.json" % (base, stamp))
    dbp.write_text(json.dumps({
        "app": "Persist", "version": 2, "root_name": root.name,
        "root_path": str(root), "scanned_at": fmt_ts(time.time()),
        "total": len(items), "items": items}))
    csvp = dbp.with_suffix(".csv")
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Path", "Name", "Type", "Size Bytes", "Taken Date",
                     "Modified Date", "SHA-256"])
        for it in items:
            wr.writerow([it["path"], it["name"], it["kind"], it["size"],
                         it["taken"], it["modified"], it["hash"]])
    kinds = {"photo": 0, "video": 0, "other": 0}
    size_sum = 0
    folders = {}
    for it in items:
        kinds[it["kind"]] += 1
        size_sum += it["size"]
        top = it["path"].split("/")[0] if "/" in it["path"] else "(root files)"
        c, s = folders.get(top, (0, 0))
        folders[top] = (c + 1, s + it["size"])
    key = lambda it: it["taken_ts"] or it["mtime"]
    old, new = min(items, key=key), max(items, key=key)
    with_taken = sum(1 for it in items if it["taken_ts"])
    app.log("SCAN COMPLETE (%.0f sec)" % (time.time() - t0))
    app.log("Folder: " + root.name)
    app.log("Files: %d  (Photos %d, Videos %d, Others %d)" %
            (len(items), kinds["photo"], kinds["video"], kinds["other"]))
    app.log("Total Size: " + human(size_sum))
    app.log("Oldest: %s  (%s)" % (fmt_ts(key(old)), old["name"]))
    app.log("Newest: %s  (%s)" % (fmt_ts(key(new)), new["name"]))
    app.log("Photos With Camera Date (EXIF): %d" % with_taken)
    app.log("Top Folders:")
    for name, (c, s) in sorted(folders.items(), key=lambda x: -x[1][0])[:10]:
        app.log("  %s: %d files, %s" % (name, c, human(s)))
    if failed:
        app.log("Could Not Read %d File(s):" % len(failed))
        for x in failed[:5]:
            app.log("  " + x)
    app.log("Database: " + str(dbp))
    app.log("Details (CSV): " + csvp.name)
    app.log("Back The Database Up To Drive.")
    return "Scanned %d files (%s)" % (len(items), human(size_sum))


# --------------------------------------------------------------- extract
def do_extract(app, zpath, dbpath, out_dir):
    t0 = time.time()
    data = json.loads(dbpath.read_text())
    if isinstance(data, dict):
        items = data["items"]
        root_name = data.get("root_name") or guess_root(dbpath.stem)
        scanned_at = data.get("scanned_at", "")
    else:
        items, scanned_at = data, ""
        root_name = guess_root(dbpath.stem)
    root_name = safe_name(root_name)
    main_dir = out_dir / root_name
    outs_dir = out_dir / (PIN + " Outsider Files - " + root_name)
    miss_dir = out_dir / (PIN + " Missing Files - " + root_name)
    tmpdir = out_dir / ".persist_tmp"
    try:
        zf0 = zipfile.ZipFile(zpath)
        infos = [i for i in zf0.infolist() if not i.is_dir()]
        zf0.close()
    except Exception as e:
        raise RuntimeError("Zip is damaged or incomplete: %r" % e)
    tmpdir.mkdir(parents=True, exist_ok=True)
    main_dir.mkdir(parents=True, exist_ok=True)

    by_hash, by_ns = {}, {}
    for it in items:
        by_hash.setdefault(it["hash"], []).append(it)
        by_ns.setdefault((Path(it["path"]).name, it["size"]), []).append(it)
    sizes = {it["size"] for it in items}
    lock = threading.Lock()
    tl = threading.local()

    def getzip():
        if not hasattr(tl, "zf"):
            tl.zf = zipfile.ZipFile(zpath)
        return tl.zf

    def work(info):
        name = Path(info.filename).name
        is_json = info.filename.lower().endswith(".json")
        need_hash = info.file_size in sizes
        if is_json and not need_hash:
            return ("skipped_json", info, None)
        tmp = tmpdir / (".t%d" % threading.get_ident())
        h = hashlib.sha256() if need_hash else None
        try:
            with getzip().open(info) as f, open(tmp, "wb") as o:
                for c in iter(lambda: f.read(4 << 20), b""):
                    if h is not None:
                        h.update(c)
                    o.write(c)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return ("damaged", info, repr(e))
        kind, hits = "outsider", None
        if h is not None:
            hits = by_hash.get(h.hexdigest())
            if hits:
                kind = "ok"
            else:
                hits = by_ns.get((name, info.file_size))
                if hits:
                    kind = "weak"
        if kind == "outsider" and is_json:
            tmp.unlink(missing_ok=True)
            return ("skipped_json", info, None)
        if kind == "outsider":
            targets = [(outs_dir, info.filename, None)]
        else:
            targets = [(main_dir, x["path"], x.get("mtime")) for x in hits]
        placed = existed = 0
        with lock:
            first = None
            for base, rel, mt in targets:
                t = safe(base, rel)
                if t is None:
                    continue
                if t.exists():
                    existed += 1
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                if first is None:
                    shutil.move(str(tmp), str(t))
                    first = t
                else:
                    shutil.copy2(first, t)
                placed += 1
                if mt:
                    try:
                        os.utime(t, (mt, mt))
                    except Exception:
                        pass
            if first is None:
                tmp.unlink(missing_ok=True)
        return (kind, info, (placed, existed,
                             hits[0]["path"] if hits else None))

    total = len(infos)
    w = app.workers()
    app.log("Extracting %d entries from %s using %d core(s)" %
            (total, zpath.name, w))
    st = {"ok": 0, "weak": 0, "restored": 0, "existed": 0, "outsider": 0,
          "json": 0, "damaged": 0}
    outsiders, weaks, damaged = [], [], []
    with ThreadPoolExecutor(max_workers=w) as ex:
        futs = {ex.submit(work, i): i for i in infos}
        for n, fu in enumerate(as_completed(futs), 1):
            try:
                kind, info, extra = fu.result()
            except Exception as e:
                kind, info, extra = "damaged", futs[fu], repr(e)
            if kind in ("ok", "weak"):
                st[kind] += 1
                st["restored"] += extra[0]
                st["existed"] += extra[1]
                if kind == "weak":
                    weaks.append((info.filename, extra[2]))
            elif kind == "outsider":
                st["outsider"] += 1
                outsiders.append((info.filename, info.file_size))
            elif kind == "skipped_json":
                st["json"] += 1
            else:
                st["damaged"] += 1
                damaged.append((info.filename, extra))
            if n % 20 == 0 or n == total:
                app.set_status("Extracting %d / %d" % (n, total),
                               n * 100.0 / max(total, 1))
    shutil.rmtree(tmpdir, ignore_errors=True)

    present = {it["path"] for it in items if (main_dir / it["path"]).exists()}
    missing = [it for it in items if it["path"] not in present]
    tot_f, got_f = {}, {}
    for it in items:
        top = it["path"].split("/")[0] if "/" in it["path"] else "(root files)"
        tot_f[top] = tot_f.get(top, 0) + 1
        if it["path"] in present:
            got_f[top] = got_f.get(top, 0) + 1

    miss_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    head = ["Database: %s" % dbpath.name,
            "Checked: %s" % fmt_ts(now),
            "Expected: %d | Present: %d | Missing: %d" %
            (len(items), len(present), len(missing)), ""]
    ml = ["PERSIST - MISSING FILES"] + head
    if not missing:
        ml.append("Nothing is missing. Every file is back.")
    for k, it in enumerate(missing, 1):
        mod = it.get("modified") or (fmt_ts(it["mtime"]) if it.get("mtime")
                                     else "-")
        ml.append("%d. %s" % (k, it["path"]))
        ml.append("   Size: %s | Taken: %s | Modified: %s" %
                  (human(it["size"]), it.get("taken") or "-", mod))
        ml.append("   SHA-256: " + it["hash"])
    (miss_dir / "missing_files.txt").write_text("\n".join(ml),
                                                encoding="utf-8")
    with open(miss_dir / "missing_files.csv", "w", newline="",
              encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Path", "Size Bytes", "Taken Date", "Modified Date",
                     "SHA-256"])
        for it in missing:
            wr.writerow([it["path"], it["size"], it.get("taken", ""),
                         it.get("modified", ""), it["hash"]])

    secs = time.time() - t0
    rl = ["PERSIST - EXTRACTION REPORT",
          "Finished: %s (%.0f sec)" % (fmt_ts(now), secs),
          "Zip: %s" % zpath, "Database: %s" % dbpath,
          "Database Scanned At: %s" % (scanned_at or "-"),
          "Main Folder: %s" % main_dir, "",
          "SUMMARY",
          "  Entries In Zip: %d" % total,
          "  Matched By Content: %d" % st["ok"],
          "  Matched By Name+Size Only: %d" % st["weak"],
          "  Files Written: %d" % st["restored"],
          "  Already Existed (Skipped): %d" % st["existed"],
          "  Outsider Files: %d" % st["outsider"],
          "  Damaged Entries: %d" % st["damaged"],
          "  Takeout JSON Skipped: %d" % st["json"],
          "  Missing Now: %d of %d" % (len(missing), len(items)), "",
          "FOLDER-WISE STATUS"]
    for top in sorted(tot_f):
        got = got_f.get(top, 0)
        rl.append("  %s: %d / %d back  (%d missing)" %
                  (top, got, tot_f[top], tot_f[top] - got))
    if outsiders:
        rl += ["", "OUTSIDER FILES (kept in: %s)" % outs_dir.name]
        rl += ["  %d. %s (%s)" % (k, n, human(s))
               for k, (n, s) in enumerate(outsiders, 1)]
    if weaks:
        rl += ["", "WEAK MATCHES (name+size equal, content differs)"]
        rl += ["  %s  ->  %s" % (a, b) for a, b in weaks]
    if damaged:
        rl += ["", "DAMAGED ENTRIES (could not be read)"]
        rl += ["  %s : %s" % (a, b) for a, b in damaged]
    (miss_dir / ("report_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
     ).write_text("\n".join(rl), encoding="utf-8")

    app.log("EXTRACTION COMPLETE (%.0f sec)" % secs)
    app.log("Zip: " + zpath.name)
    app.log("Restored Into: " + str(main_dir))
    app.log("Matched: %d (Content %d, Name+Size %d)" %
            (st["ok"] + st["weak"], st["ok"], st["weak"]))
    app.log("Files Written: %d | Already Existed: %d" %
            (st["restored"], st["existed"]))
    app.log("Takeout JSON Skipped: %d" % st["json"])
    app.log("Outsider Files: %d" % st["outsider"])
    if outsiders:
        app.log("  Kept In: " + disp(outs_dir.name))
        for n, s in outsiders[:8]:
            app.log("  - %s (%s)" % (n, human(s)))
        if len(outsiders) > 8:
            app.log("  ...and %d more (see report)" % (len(outsiders) - 8))
    app.log("Missing Now: %d of %d" % (len(missing), len(items)))
    if missing:
        app.log("  Note In: " + disp(miss_dir.name))
        for it in missing[:8]:
            app.log("  - %s (%s)" % (it["path"], human(it["size"])))
        if len(missing) > 8:
            app.log("  ...and %d more (see missing_files.txt)" %
                    (len(missing) - 8))
        app.log("  (Normal If More Takeout Zips Are Left)")
    if st["damaged"]:
        app.log("Damaged Entries: %d (see report)" % st["damaged"])
    app.log("Report Saved In: " + disp(miss_dir.name))
    return "Written %d | Outsiders %d | Missing %d" % (
        st["restored"], st["outsider"], len(missing))


# ------------------------------------------------------------------- UI
def wlabel(text="", **kw):
    lb = MDLabel(text=text, size_hint_y=None, halign="left", **kw)
    lb.bind(width=lambda *a: setattr(lb, "text_size", (lb.width, None)),
            texture_size=lambda *a: setattr(
                lb, "height", max(dp(24), lb.texture_size[1])))
    return lb


class Picker(ModalView):
    def __init__(self, mode, start, title, on_pick, **kw):
        super().__init__(size_hint=(0.96, 0.92), auto_dismiss=False,
                         background_color=(0, 0, 0, 0), **kw)
        self.mode, self.cur, self.on_pick = mode, Path(start), on_pick
        body = MDBoxLayout(orientation="vertical", padding=dp(8),
                           spacing=dp(6), md_bg_color=(0.1, 0.1, 0.13, 1))
        body.add_widget(MDLabel(text=title, font_style="H6",
                                size_hint_y=None, height=dp(36)))
        self.path_lbl = MDLabel(text="", font_style="Caption",
                                size_hint_y=None, height=dp(34))
        body.add_widget(self.path_lbl)
        sv = MDScrollView()
        self.items = MDList()
        sv.add_widget(self.items)
        body.add_widget(sv)
        bar = MDBoxLayout(size_hint_y=None, height=dp(52), spacing=dp(4))
        bar.add_widget(MDFlatButton(text="Up",
                                    on_release=lambda *_: self.go_up()))
        if mode == "dir":
            bar.add_widget(MDFlatButton(
                text="New", on_release=lambda *_: self.new_folder()))
            bar.add_widget(MDRaisedButton(
                text="Use This", on_release=lambda *_: self.choose(self.cur)))
        bar.add_widget(MDFlatButton(text="Cancel",
                                    on_release=lambda *_: self.dismiss()))
        body.add_widget(bar)
        self.add_widget(body)
        self.refresh()

    def go_up(self):
        if self.cur != ROOT:
            self.cur = self.cur.parent
            self.refresh()

    def go(self, p):
        self.cur = p
        self.refresh()

    def choose(self, p):
        self.dismiss()
        self.on_pick(p)

    def new_folder(self):
        tf = MDTextField(hint_text="Folder Name")

        def ok(*_):
            name = safe_name(tf.text) if tf.text.strip() else ""
            if name:
                p = self.cur / name
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    self.go(p)
                except Exception:
                    pass
            dlg.dismiss()

        dlg = MDDialog(
            title="New Folder", type="custom", content_cls=tf,
            buttons=[MDFlatButton(text="Cancel",
                                  on_release=lambda *_: dlg.dismiss()),
                     MDFlatButton(text="Create", on_release=ok)])
        dlg.open()

    def refresh(self):
        self.path_lbl.text = str(self.cur)
        self.items.clear_widgets()
        dirs, files = [], []
        try:
            with os.scandir(self.cur) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir():
                            dirs.append(e.name)
                        elif (self.mode != "dir"
                              and e.name.lower().endswith(self.mode)):
                            files.append((e.name, e.stat().st_size))
                    except OSError:
                        pass
        except OSError:
            pass
        for name in sorted(dirs, key=str.lower):
            self.items.add_widget(OneLineListItem(
                text="[DIR]  " + name,
                on_release=lambda w, n=name: self.go(self.cur / n)))
        for name, size in sorted(files, key=lambda x: x[0].lower()):
            self.items.add_widget(OneLineListItem(
                text="%s  (%s)" % (name, human(size)),
                on_release=lambda w, n=name: self.choose(self.cur / n)))
        if not dirs and not files:
            self.items.add_widget(OneLineListItem(text="(Nothing Here)"))


class PersistApp(MDApp):
    title = "Persist"

    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Indigo"
        self.busy = False
        self.lines = []
        self._wl = None
        self.cfg = self.load_cfg()
        self.sm = MDScreenManager()
        self.sm.add_widget(self.build_home())
        self.sm.add_widget(self.build_settings())
        self.refresh_info()
        return self.sm

    # ---- settings storage
    def cfg_path(self):
        return Path(self.user_data_dir) / "persist_settings.json"

    def load_cfg(self):
        n = os.cpu_count() or 2
        cfg = {"db_dir": "", "out_dir": "", "workers": min(4, n),
               "notify": True, "vibrate": True, "battery_done": False}
        try:
            cfg.update(json.loads(self.cfg_path().read_text()))
        except Exception:
            pass
        return cfg

    def set_cfg(self, k, v):
        self.cfg[k] = v
        try:
            self.cfg_path().write_text(json.dumps(self.cfg))
        except Exception:
            pass
        self.refresh_info()

    def workers(self):
        n = os.cpu_count() or 2
        try:
            return max(1, min(int(self.cfg.get("workers", 4)), n))
        except Exception:
            return 1

    # ---- screens
    def card(self):
        return MDBoxLayout(orientation="vertical", adaptive_height=True,
                           padding=dp(14), spacing=dp(10),
                           md_bg_color=CARD_BG, radius=[dp(16)])

    def build_home(self):
        scr = MDScreen(name="home")
        root = MDBoxLayout(orientation="vertical")
        root.add_widget(MDTopAppBar(
            title="Persist",
            right_action_items=[["cog", lambda x: self.goto("settings")]]))
        sv = MDScrollView()
        col = MDBoxLayout(orientation="vertical", adaptive_height=True,
                          padding=dp(12), spacing=dp(12))
        c = self.card()
        self.info_lbl = wlabel("")
        c.add_widget(self.info_lbl)
        col.add_widget(c)
        col.add_widget(MDFillRoundFlatButton(
            text="Scan A Folder", size_hint=(1, None), height=dp(56),
            on_release=lambda x: self.start_scan()))
        col.add_widget(MDFillRoundFlatButton(
            text="Extract A Zip", size_hint=(1, None), height=dp(56),
            on_release=lambda x: self.start_extract()))
        self.status_lbl = wlabel("Ready", halign="center")
        col.add_widget(self.status_lbl)
        self.bar = MDProgressBar(value=0, size_hint_y=None, height=dp(6))
        col.add_widget(self.bar)
        self.log_lbl = wlabel("")
        col.add_widget(self.log_lbl)
        sv.add_widget(col)
        root.add_widget(sv)
        scr.add_widget(root)
        return scr

    def build_settings(self):
        scr = MDScreen(name="settings")
        root = MDBoxLayout(orientation="vertical")
        root.add_widget(MDTopAppBar(
            title="Settings",
            left_action_items=[["arrow-left", lambda x: self.goto("home")]]))
        sv = MDScrollView()
        col = MDBoxLayout(orientation="vertical", adaptive_height=True,
                          padding=dp(12), spacing=dp(12))

        c = self.card()
        c.add_widget(wlabel("Database Folder", font_style="Subtitle1"))
        self.db_lbl = wlabel("")
        c.add_widget(self.db_lbl)
        c.add_widget(MDRaisedButton(
            text="Choose Database Folder",
            on_release=lambda x: self.choose_folder(
                "db_dir", "Choose Database Folder")))
        col.add_widget(c)

        c = self.card()
        c.add_widget(wlabel("Output Folder", font_style="Subtitle1"))
        self.out_lbl = wlabel("")
        c.add_widget(self.out_lbl)
        c.add_widget(MDRaisedButton(
            text="Choose Output Folder",
            on_release=lambda x: self.choose_folder(
                "out_dir", "Choose Output Folder")))
        col.add_widget(c)

        c = self.card()
        c.add_widget(wlabel("CPU Cores To Use", font_style="Subtitle1"))
        self.cpu_lbl = wlabel("")
        c.add_widget(self.cpu_lbl)
        n = os.cpu_count() or 2
        opts = sorted(set([k for k in (1, 2, 3, 4, 6, 8) if k < n] + [n]))
        self.cpu_btns = {}
        row = MDBoxLayout(adaptive_height=True, spacing=dp(6))
        for k in opts:
            b = MDRaisedButton(
                text="All" if k == n else str(k),
                on_release=lambda w, k=k: self.set_cfg("workers", k))
            self.cpu_btns[k] = b
            row.add_widget(b)
        c.add_widget(row)
        col.add_widget(c)

        c = self.card()
        c.add_widget(self.switch_row("Notification When Finished", "notify"))
        c.add_widget(self.switch_row("Vibrate When Finished", "vibrate"))
        col.add_widget(c)

        c = self.card()
        c.add_widget(wlabel("Background Protection", font_style="Subtitle1"))
        c.add_widget(wlabel("Stops Android From Killing Persist While "
                            "It Works."))
        c.add_widget(MDRaisedButton(
            text="Remove Battery Restriction",
            on_release=lambda x: self.battery_dialog()))
        col.add_widget(c)

        sv.add_widget(col)
        root.add_widget(sv)
        scr.add_widget(root)
        return scr

    def switch_row(self, text, key):
        row = MDBoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        row.add_widget(MDLabel(text=text, valign="middle"))
        sw = MDSwitch(active=bool(self.cfg.get(key, True)),
                      size_hint=(None, None), pos_hint={"center_y": 0.5})
        sw.bind(active=lambda inst, v: self.set_cfg(key, bool(v)))
        row.add_widget(sw)
        return row

    def goto(self, name):
        self.sm.current = name

    def refresh_info(self):
        if not hasattr(self, "cpu_btns"):
            return
        c = self.cfg
        n = os.cpu_count() or 2
        dbt = c["db_dir"] or "Not Set (Asked On First Scan)"
        outt = c["out_dir"] or "Not Set (Asked On First Extract)"
        self.info_lbl.text = (
            "Database Folder: %s\nOutput Folder: %s\nCPU Cores: %d Of %d\n"
            "Notification: %s | Vibration: %s" %
            (dbt, outt, self.workers(), n,
             "On" if c["notify"] else "Off",
             "On" if c["vibrate"] else "Off"))
        self.db_lbl.text = dbt
        self.out_lbl.text = outt
        self.cpu_lbl.text = "Using %d Of %d Cores" % (self.workers(), n)
        for k, b in self.cpu_btns.items():
            b.md_bg_color = (self.theme_cls.primary_color
                             if k == self.workers() else (0.3, 0.3, 0.36, 1))

    # ---- logging
    def add_log(self, msg):
        self.lines.append(msg)
        self.lines = self.lines[-400:]
        self.log_lbl.text = "\n".join(self.lines)

    def log(self, msg):
        Clock.schedule_once(lambda dt: self.add_log(msg))

    def set_status(self, msg, pct=None):
        def f(dt):
            self.status_lbl.text = msg
            if pct is not None:
                self.bar.value = pct
        Clock.schedule_once(f)

    # ---- android lifecycle
    def on_start(self):
        Clock.schedule_once(lambda dt: ask_access(), 1)
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread
            act = _act()
            WM = autoclass("android.view.WindowManager$LayoutParams")

            @run_on_ui_thread
            def keep_on():
                act.getWindow().addFlags(WM.FLAG_KEEP_SCREEN_ON)
            keep_on()
        except Exception:
            pass
        if not self.cfg.get("battery_done"):
            Clock.schedule_once(lambda dt: self.battery_dialog(True), 4)

    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def hold_cpu(self, on):
        try:
            if on:
                from jnius import autoclass
                Context = autoclass("android.content.Context")
                PM = autoclass("android.os.PowerManager")
                pm = _act().getSystemService(Context.POWER_SERVICE)
                self._wl = pm.newWakeLock(PM.PARTIAL_WAKE_LOCK, "Persist:job")
                self._wl.acquire(6 * 3600 * 1000)
            elif self._wl is not None:
                if self._wl.isHeld():
                    self._wl.release()
                self._wl = None
        except Exception:
            pass

    # ---- dialogs
    def battery_dialog(self, first=False):
        if first:
            self.set_cfg("battery_done", True)
        dlg = None

        def close(*_):
            dlg.dismiss()

        def allow(*_):
            r = request_battery_exemption()
            if r == "Already Unrestricted":
                self.set_status("Battery Already Unrestricted")
            dlg.dismiss()

        def appset(*_):
            open_app_settings()
            dlg.dismiss()

        dlg = MDDialog(
            title="Keep Persist Running",
            text=("Android Can Stop Persist In The Background.\n\n"
                  "1. Tap Allow Now And Choose Allow.\n"
                  "2. Tap App Settings, Open Battery Saver And Pick "
                  "No Restrictions.\n"
                  "3. Turn On Autostart (Xiaomi / MIUI).\n"
                  "4. Lock Persist In Recent Apps."),
            buttons=[MDFlatButton(text="Later", on_release=close),
                     MDFlatButton(text="App Settings", on_release=appset),
                     MDFlatButton(text="Allow Now", on_release=allow)])
        dlg.open()

    def ensure_dir(self, key, title, then):
        cur = self.cfg.get(key) or ""
        if cur and Path(cur).is_dir():
            then(Path(cur))
            return

        def picked(p):
            self.set_cfg(key, str(p))
            then(p)
        Picker("dir", ROOT, title, picked).open()

    def choose_folder(self, key, title):
        Picker("dir", ROOT, title,
               lambda p: self.set_cfg(key, str(p))).open()

    # ---- flows
    def start_scan(self):
        if self.busy:
            self.set_status("A Job Is Already Running")
            return

        def with_db(db_dir):
            Picker("dir", ROOT, "Pick The Folder To Scan",
                   lambda p: self.run_job("Scan", do_scan, p, db_dir)).open()
        self.ensure_dir("db_dir", "Choose Where To Save The Database",
                        with_db)

    def start_extract(self):
        if self.busy:
            self.set_status("A Job Is Already Running")
            return

        def got_zip(z):
            dbd = self.cfg.get("db_dir")
            start = Path(dbd) if dbd and Path(dbd).is_dir() else ROOT

            def got_db(d):
                self.ensure_dir("out_dir", "Choose Output Folder",
                                lambda o: self.confirm_extract(z, d, o))
            Picker(".json", start, "Pick The Database (.json)",
                   got_db).open()
        Picker(".zip", ROOT, "Pick The Takeout Zip", got_zip).open()

    def confirm_extract(self, z, d, out):
        dlg = None

        def start(*_):
            dlg.dismiss()
            self.run_job("Extract", do_extract, z, d, out)

        def change(*_):
            dlg.dismiss()

            def picked(p):
                self.set_cfg("out_dir", str(p))
                self.confirm_extract(z, d, p)
            Picker("dir", ROOT, "Choose Output Folder", picked).open()

        dlg = MDDialog(
            title="Start Extraction?",
            text=("Zip: %s\nDatabase: %s\nOutput: %s\n\n"
                  "Nothing Existing Is Overwritten." %
                  (z.name, d.name, out)),
            buttons=[MDFlatButton(text="Cancel",
                                  on_release=lambda *_: dlg.dismiss()),
                     MDFlatButton(text="Change Output", on_release=change),
                     MDRaisedButton(text="Start", on_release=start)])
        dlg.open()

    def run_job(self, title, fn, *args):
        if self.busy:
            return
        self.busy = True
        self.lines = []
        self.log_lbl.text = ""
        self.hold_cpu(True)

        def work():
            ok, msg = True, ""
            try:
                msg = fn(self, *args)
            except RuntimeError as e:
                ok, msg = False, str(e)
                self.log("ERROR: " + msg)
            except Exception as e:
                ok, msg = False, repr(e)
                self.log("ERROR: " + traceback.format_exc()[-700:])
            finally:
                self.hold_cpu(False)
                self.busy = False
                self.set_status("Ready", 0)
            if self.cfg.get("notify"):
                notify("Persist - %s %s" %
                       (title, "Finished" if ok else "Failed"), msg)
            if self.cfg.get("vibrate"):
                vibrate(ok)
        threading.Thread(target=work, daemon=True).start()
