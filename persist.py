import os, re, csv, json, time, shutil, struct, zipfile, hashlib
import threading, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from kivy.animation import Animation
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.gridlayout import GridLayout
from kivy.uix.modalview import ModalView
from kivy.uix.screenmanager import FadeTransition
from kivy.utils import get_color_from_hex
from kivymd.app import MDApp
from kivymd.color_definitions import colors as MD_COLORS
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import (MDFlatButton, MDIconButton, MDRaisedButton)
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDIcon, MDLabel
from kivymd.uix.list import (IconLeftWidget, MDList, OneLineIconListItem,
                             OneLineListItem, TwoLineIconListItem)
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.screen import MDScreen
from kivymd.uix.screenmanager import MDScreenManager
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.slider import MDSlider
from kivymd.uix.textfield import MDTextField
from kivymd.uix.toolbar import MDTopAppBar

try:
    from kivymd.uix.transition import MDFadeSlideTransition
except Exception:
    MDFadeSlideTransition = None

ROOT = Path("/storage/emulated/0")
PIN = "\U0001F4CC"
DATE_FMT = "%d-%b-%Y %H:%M:%S"
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
             ".bmp", ".dng", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".3gp", ".webm", ".m4v",
             ".mpg", ".mpeg"}
PALETTES = ["Indigo", "Blue", "Teal", "Green", "Orange", "DeepOrange",
            "Pink", "Purple"]
PLACEHOLDER = "Results Will Appear Here After A Scan Or Extraction."


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


def pretty(p):
    return str(p).replace("/storage/emulated/0", "Internal Storage", 1)


def lowprio():
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:
        pass


def docid_to_path(doc, want_dir):
    try:
        if doc.startswith("raw:"):
            p = Path(doc[4:])
        else:
            vol, _, rel = doc.partition(":")
            base = ROOT if vol == "primary" else Path("/storage") / vol
            p = base / rel
        ok = p.is_dir() if want_dir else p.is_file()
        return p if ok else None
    except Exception:
        return None


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


def battery_ok():
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        act = _act()
        pm = act.getSystemService(Context.POWER_SERVICE)
        return bool(pm.isIgnoringBatteryOptimizations(act.getPackageName()))
    except Exception:
        return True


def request_battery_exemption():
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        act = _act()
        i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
        i.setData(Uri.parse("package:" + act.getPackageName()))
        act.startActivity(i)
    except Exception:
        open_app_settings()


def set_hi_refresh():
    try:
        from android.runnable import run_on_ui_thread
        act = _act()

        @run_on_ui_thread
        def go():
            try:
                win = act.getWindow()
                lp = win.getAttributes()
                disp_ = act.getWindowManager().getDefaultDisplay()
                cur = disp_.getMode()
                best = None
                for m in disp_.getSupportedModes():
                    if (m.getPhysicalWidth() != cur.getPhysicalWidth()
                            or m.getPhysicalHeight()
                            != cur.getPhysicalHeight()):
                        continue
                    if best is None or m.getRefreshRate() > best.getRefreshRate():
                        best = m
                if best is not None:
                    lp.preferredDisplayModeId = best.getModeId()
                    lp.preferredRefreshRate = float(best.getRefreshRate())
                    win.setAttributes(lp)
            except Exception:
                pass
        go()
    except Exception:
        pass


def display_hz():
    try:
        return int(round(_act().getWindowManager().getDefaultDisplay()
                         .getRefreshRate()))
    except Exception:
        return 0


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
    with ThreadPoolExecutor(max_workers=w, initializer=lowprio) as ex:
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
    app.log("Database: " + pretty(dbp))
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
    with ThreadPoolExecutor(max_workers=w, initializer=lowprio) as ex:
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
    ml = ["PERSIST - MISSING FILES", "Database: %s" % dbpath.name,
          "Checked: %s" % fmt_ts(now),
          "Expected: %d | Present: %d | Missing: %d" %
          (len(items), len(present), len(missing)), ""]
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
    app.log("Restored Into: " + pretty(main_dir))
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
    kw.setdefault("halign", "left")
    lb = MDLabel(text=text, size_hint_y=None, **kw)
    lb.bind(width=lambda *a: setattr(lb, "text_size", (lb.width, None)),
            texture_size=lambda *a: setattr(
                lb, "height", max(dp(22), lb.texture_size[1])))
    return lb


def fade_in(widgets, step=0.07):
    for k, w in enumerate(widgets):
        w.opacity = 0
        Clock.schedule_once(
            lambda dt, w=w: Animation(opacity=1, d=0.5, t="out_cubic")
            .start(w), 0.05 + k * step)


def make_tap(widget, cb):
    st = {}

    def down(w, t):
        if w.collide_point(*t.pos):
            st["p"] = t.pos
            Animation.cancel_all(w, "opacity")
            Animation(opacity=0.78, d=0.08, t="out_quad").start(w)
        return False

    def up(w, t):
        p = st.pop("p", None)
        if p is not None:
            Animation.cancel_all(w, "opacity")
            Animation(opacity=1, d=0.28, t="out_cubic").start(w)
            if (w.collide_point(*t.pos) and abs(t.pos[0] - p[0]) < dp(14)
                    and abs(t.pos[1] - p[1]) < dp(14)):
                cb()
        return False

    widget.bind(on_touch_down=down, on_touch_up=up)


class Picker(ModalView):
    def __init__(self, mode, start, title, on_pick, **kw):
        super().__init__(size_hint=(0.96, 0.92), auto_dismiss=False,
                         background_color=(0, 0, 0, 0), **kw)
        self.app = MDApp.get_running_app()
        self.mode, self.cur, self.on_pick = mode, Path(start), on_pick
        if not self.cur.is_dir():
            self.cur = ROOT
        body = MDBoxLayout(orientation="vertical", padding=dp(14),
                           spacing=dp(8), md_bg_color=self.app.screen_bg(),
                           radius=[dp(24)])
        body.add_widget(wlabel(title, font_style="H6"))
        self.path_lbl = wlabel("", font_style="Caption",
                               theme_text_color="Secondary")
        body.add_widget(self.path_lbl)
        chips = MDScrollView(do_scroll_x=True, do_scroll_y=False,
                             size_hint_y=None, height=dp(48))
        row = MDBoxLayout(adaptive_width=True, spacing=dp(8),
                          size_hint_y=None, height=dp(44))
        for name, p in self.quick_places():
            row.add_widget(MDRaisedButton(
                text=name, on_release=lambda w, p=p: self.go(p)))
        chips.add_widget(row)
        body.add_widget(chips)
        sv = MDScrollView()
        self.items = MDList()
        sv.add_widget(self.items)
        body.add_widget(sv)
        bar = MDBoxLayout(size_hint_y=None, height=dp(56), spacing=dp(2))
        bar.add_widget(MDIconButton(icon="arrow-up",
                                    on_release=lambda *_: self.go_up()))
        if mode == "dir":
            bar.add_widget(MDIconButton(
                icon="folder-plus", on_release=lambda *_: self.new_folder()))
        bar.add_widget(MDIconButton(icon="open-in-app",
                                    on_release=lambda *_: self.system()))
        bar.add_widget(MDLabel(text=""))
        bar.add_widget(MDFlatButton(text="Cancel",
                                    on_release=lambda *_: self.dismiss()))
        if mode == "dir":
            bar.add_widget(MDRaisedButton(
                text="Use This", on_release=lambda *_: self.choose(self.cur)))
        body.add_widget(bar)
        self.add_widget(body)
        self.refresh()

    def open(self, *a, **k):
        self.opacity = 0
        super().open(*a, **k)
        Animation(opacity=1, d=0.28, t="out_cubic").start(self)

    def quick_places(self):
        out = [("Internal", ROOT)]
        for n in ("Download", "DCIM", "Pictures", "Documents"):
            if (ROOT / n).is_dir():
                out.append((n, ROOT / n))
        try:
            for p in Path("/storage").iterdir():
                if p.name not in ("emulated", "self") and p.is_dir():
                    out.append(("SD Card", p))
                    break
        except Exception:
            pass
        return out

    def go_up(self):
        if self.cur != ROOT and self.cur.parent != Path("/storage"):
            self.cur = self.cur.parent
            self.refresh()

    def go(self, p):
        self.cur = p
        self.refresh()

    def choose(self, p):
        self.dismiss()
        self.on_pick(p)

    def system(self):
        kind = "dir" if self.mode == "dir" else "file"
        self.app.system_pick(kind, self.sys_done)

    def sys_done(self, p):
        if self.mode != "dir" and not p.name.lower().endswith(self.mode):
            self.app.set_status("Please Pick A %s File" % self.mode)
            return
        self.choose(p)

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
        self.path_lbl.text = pretty(self.cur)
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
                            s = e.stat()
                            files.append((e.name, s.st_size, s.st_mtime))
                    except OSError:
                        pass
        except OSError:
            pass
        for name in sorted(dirs, key=str.lower):
            item = OneLineIconListItem(
                text=name,
                on_release=lambda w, n=name: self.go(self.cur / n))
            item.add_widget(IconLeftWidget(icon="folder"))
            self.items.add_widget(item)
        icon = "zip-box" if self.mode == ".zip" else "code-json"
        for name, size, mt in sorted(files, key=lambda x: x[0].lower()):
            item = TwoLineIconListItem(
                text=name,
                secondary_text="%s  |  %s" % (human(size), fmt_ts(mt)),
                on_release=lambda w, n=name: self.choose(self.cur / n))
            item.add_widget(IconLeftWidget(icon=icon))
            self.items.add_widget(item)
        if not dirs and not files:
            self.items.add_widget(OneLineListItem(text="(Nothing Here)"))


class PersistApp(MDApp):
    title = "Persist"

    # ---- build
    def build(self):
        self.cfg = self.load_cfg()
        self.busy = False
        self.lines = []
        self._wl = None
        self._sys_cb = None
        self._last_ui = 0.0
        self.cards, self.accent_labels, self.accent_icons = [], [], []
        self.toggles, self.swatches = [], {}
        self.theme_cls.material_style = "M3"
        self.apply_theme(first=True)
        try:
            tr = MDFadeSlideTransition(duration=0.3)
        except Exception:
            tr = FadeTransition(duration=0.25)
        self.sm = MDScreenManager(transition=tr)
        self.home = self.build_home()
        self.settings = self.build_settings()
        self.sm.add_widget(self.home)
        self.sm.add_widget(self.settings)
        self.retheme()
        return self.sm

    # ---- settings storage
    def cfg_path(self):
        return Path(self.user_data_dir) / "persist_settings.json"

    def load_cfg(self):
        n = os.cpu_count() or 2
        cfg = {"db_dir": "", "last_out": "", "workers": min(4, n),
               "notify": True, "vibrate": True, "theme": "auto",
               "palette": "Indigo"}
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

    # ---- theme
    def system_dark(self):
        try:
            return (_act().getResources().getConfiguration().uiMode
                    & 0x30) == 0x20
        except Exception:
            return True

    def apply_theme(self, first=False):
        mode = self.cfg.get("theme", "auto")
        dark = mode == "dark" or (mode == "auto" and self.system_dark())
        self.theme_cls.theme_style = "Dark" if dark else "Light"
        try:
            self.theme_cls.primary_palette = self.cfg.get("palette", "Indigo")
        except Exception:
            self.theme_cls.primary_palette = "Indigo"
        if not first:
            self.retheme()

    def set_theme(self, mode):
        self.set_cfg("theme", mode)
        self.apply_theme()

    def set_palette(self, name):
        self.set_cfg("palette", name)
        self.apply_theme()

    def card_bg(self):
        return ((0.14, 0.14, 0.18, 1) if self.theme_cls.theme_style == "Dark"
                else (1, 1, 1, 1))

    def screen_bg(self):
        return ((0.07, 0.07, 0.09, 1) if self.theme_cls.theme_style == "Dark"
                else (0.94, 0.94, 0.97, 1))

    def style_toggle(self, btn, sel):
        dark = self.theme_cls.theme_style == "Dark"
        btn.md_bg_color = (self.theme_cls.primary_color if sel else
                           ((0.28, 0.28, 0.33, 1) if dark
                            else (0.88, 0.88, 0.92, 1)))
        btn.theme_text_color = "Custom"
        btn.text_color = ((1, 1, 1, 1) if (sel or dark)
                          else (0.1, 0.1, 0.12, 1))

    def retheme(self):
        if not hasattr(self, "home"):
            return
        for c in self.cards:
            c.md_bg_color = self.card_bg()
        for s in (self.home, self.settings):
            s.md_bg_color = self.screen_bg()
        for w in self.accent_labels + self.accent_icons:
            w.text_color = self.theme_cls.primary_color
        self.refresh_info()

    # ---- widget factories
    def make_card(self, auto=False, **kw):
        c = MDCard(size_hint_y=None, radius=[dp(20)], elevation=0,
                   md_bg_color=self.card_bg(), **kw)
        if auto:
            c.bind(minimum_height=c.setter("height"))
        self.cards.append(c)
        return c

    def title_lbl(self, text):
        lb = wlabel(text, font_style="Subtitle2", theme_text_color="Custom",
                    text_color=self.theme_cls.primary_color)
        self.accent_labels.append(lb)
        return lb

    def icon(self, name, size="30sp"):
        ic = MDIcon(icon=name, theme_text_color="Custom",
                    text_color=self.theme_cls.primary_color,
                    font_size=size, size_hint=(None, 1), width=dp(44),
                    halign="center", valign="middle")
        self.accent_icons.append(ic)
        return ic

    def action_card(self, icon, title, sub, cb):
        card = self.make_card(orientation="horizontal", height=dp(92),
                              padding=dp(18), spacing=dp(14))
        card.add_widget(self.icon(icon, "34sp"))
        box = MDBoxLayout(orientation="vertical", spacing=dp(2))
        box.add_widget(MDLabel(text=title, font_style="Subtitle1",
                               valign="bottom"))
        box.add_widget(MDLabel(text=sub, font_style="Caption",
                               theme_text_color="Secondary", valign="top"))
        card.add_widget(box)
        card.add_widget(MDIcon(icon="chevron-right",
                               theme_text_color="Hint",
                               size_hint=(None, 1), width=dp(28),
                               valign="middle"))
        make_tap(card, cb)
        return card

    # ---- screens
    def build_home(self):
        scr = MDScreen(name="home")
        root = MDBoxLayout(orientation="vertical")
        root.add_widget(MDTopAppBar(
            title="Persist", anchor_title="left", elevation=0,
            right_action_items=[["cog", lambda x: self.goto("settings")]]))
        sv = MDScrollView()
        col = MDBoxLayout(orientation="vertical", adaptive_height=True,
                          padding=dp(16), spacing=dp(14))

        db = self.make_card(orientation="horizontal", height=dp(88),
                            padding=dp(18), spacing=dp(14))
        db.add_widget(self.icon("database-outline"))
        box = MDBoxLayout(orientation="vertical", spacing=dp(2))
        box.add_widget(MDLabel(text="Database Folder", font_style="Caption",
                               theme_text_color="Secondary",
                               valign="bottom"))
        self.db_home_lbl = MDLabel(text="", font_style="Subtitle2",
                                   valign="top", shorten=True,
                                   shorten_from="left")
        self.db_home_lbl.bind(
            width=lambda *a: setattr(self.db_home_lbl, "text_size",
                                     (self.db_home_lbl.width, None)))
        box.add_widget(self.db_home_lbl)
        db.add_widget(box)
        db.add_widget(MDIcon(icon="pencil-outline", theme_text_color="Hint",
                             size_hint=(None, 1), width=dp(28),
                             valign="middle"))
        make_tap(db, lambda: self.choose_folder(
            "db_dir", "Choose Database Folder"))

        scan = self.action_card("folder-search-outline", "Scan A Folder",
                                "Save Every File, Folder And Date",
                                self.start_scan)
        ext = self.action_card("zip-box-outline", "Extract A Zip",
                               "Rebuild Your Folder Tree From A Takeout Zip",
                               self.start_extract)

        st = self.make_card(auto=True, orientation="vertical",
                            padding=dp(18), spacing=dp(10))
        self.status_lbl = wlabel("Ready", font_style="Subtitle2")
        st.add_widget(self.status_lbl)
        self.bar = MDProgressBar(value=0, size_hint_y=None, height=dp(6))
        st.add_widget(self.bar)

        lg = self.make_card(auto=True, orientation="vertical",
                            padding=dp(18), spacing=dp(8))
        lg.add_widget(self.title_lbl("Details"))
        self.log_lbl = wlabel(PLACEHOLDER, font_style="Body2")
        lg.add_widget(self.log_lbl)

        self.home_cards = [db, scan, ext, st, lg]
        for w in self.home_cards:
            col.add_widget(w)
        sv.add_widget(col)
        root.add_widget(sv)
        scr.add_widget(root)
        return scr

    def build_settings(self):
        scr = MDScreen(name="settings")
        root = MDBoxLayout(orientation="vertical")
        root.add_widget(MDTopAppBar(
            title="Settings", anchor_title="left", elevation=0,
            left_action_items=[["arrow-left", lambda x: self.goto("home")]]))
        sv = MDScrollView()
        col = MDBoxLayout(orientation="vertical", adaptive_height=True,
                          padding=dp(16), spacing=dp(14))

        # appearance
        c = self.make_card(auto=True, orientation="vertical",
                           padding=dp(18), spacing=dp(10))
        c.add_widget(self.title_lbl("Appearance"))
        c.add_widget(wlabel("Mode", font_style="Caption",
                            theme_text_color="Secondary"))
        row = MDBoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.mode_btns = {}
        for key, label in (("auto", "Auto"), ("dark", "Dark"),
                           ("light", "Light")):
            b = MDRaisedButton(
                text=label, on_release=lambda w, k=key: self.set_theme(k))
            self.mode_btns[key] = b
            row.add_widget(b)
        c.add_widget(row)
        c.add_widget(wlabel("Accent Color", font_style="Caption",
                            theme_text_color="Secondary"))
        grid = GridLayout(cols=4, size_hint_y=None, row_default_height=dp(52),
                          row_force_default=True, spacing=dp(6))
        grid.bind(minimum_height=grid.setter("height"))
        for name in PALETTES:
            try:
                col_rgba = get_color_from_hex(MD_COLORS[name]["500"])
            except Exception:
                continue
            b = MDIconButton(icon="checkbox-blank-circle",
                             theme_text_color="Custom", text_color=col_rgba,
                             user_font_size="40sp",
                             on_release=lambda w, n=name: self.set_palette(n))
            self.swatches[name] = b
            grid.add_widget(b)
        c.add_widget(grid)
        col.add_widget(c)

        # performance
        c = self.make_card(auto=True, orientation="vertical",
                           padding=dp(18), spacing=dp(8))
        c.add_widget(self.title_lbl("Performance"))
        self.cpu_lbl = wlabel("", font_style="Subtitle1")
        c.add_widget(self.cpu_lbl)
        n = max(os.cpu_count() or 2, 2)
        sl = MDSlider(min=1, max=n, value=self.workers(), step=1,
                      size_hint_y=None, height=dp(44))
        sl.bind(value=lambda i, v: self.set_cfg("workers", int(round(v))))
        c.add_widget(sl)
        c.add_widget(wlabel("More Cores Is Faster But Warms The Phone.",
                            font_style="Caption",
                            theme_text_color="Secondary"))
        self.hz_lbl = wlabel("", font_style="Caption",
                             theme_text_color="Secondary")
        c.add_widget(self.hz_lbl)
        col.add_widget(c)

        # alerts
        c = self.make_card(auto=True, orientation="vertical",
                           padding=dp(18), spacing=dp(4))
        c.add_widget(self.title_lbl("Alerts"))
        c.add_widget(self.switch_row("Notification When Finished", "notify"))
        c.add_widget(self.switch_row("Vibrate When Finished", "vibrate"))
        col.add_widget(c)

        # battery
        c = self.make_card(auto=True, orientation="vertical",
                           padding=dp(18), spacing=dp(8))
        c.add_widget(self.title_lbl("Background Protection"))
        row = MDBoxLayout(size_hint_y=None, height=dp(56), spacing=dp(10))
        self.batt_icon = MDIcon(icon="check-circle", theme_text_color="Custom",
                                text_color=(0.3, 0.78, 0.4, 1),
                                font_size="30sp", size_hint=(None, 1),
                                width=dp(40), halign="center",
                                valign="middle")
        row.add_widget(self.batt_icon)
        self.batt_lbl = MDLabel(text="", font_style="Subtitle1",
                                valign="middle")
        row.add_widget(self.batt_lbl)
        self.batt_btn = MDRaisedButton(
            text="Allow", pos_hint={"center_y": 0.5},
            on_release=lambda x: self.battery_dialog())
        row.add_widget(self.batt_btn)
        c.add_widget(row)
        c.add_widget(wlabel("Autostart And Recents Lock Cannot Be Detected. "
                            "Check Them In App Settings.",
                            font_style="Caption",
                            theme_text_color="Secondary"))
        c.add_widget(MDFlatButton(text="App Settings",
                                  on_release=lambda x: open_app_settings()))
        col.add_widget(c)

        # database
        c = self.make_card(auto=True, orientation="vertical",
                           padding=dp(18), spacing=dp(8))
        c.add_widget(self.title_lbl("Database Folder"))
        self.db_lbl = wlabel("", font_style="Body2")
        c.add_widget(self.db_lbl)
        c.add_widget(MDRaisedButton(
            text="Choose Folder",
            on_release=lambda x: self.choose_folder(
                "db_dir", "Choose Database Folder")))
        col.add_widget(c)

        sv.add_widget(col)
        root.add_widget(sv)
        scr.add_widget(root)
        return scr

    def switch_row(self, text, key):
        row = MDBoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        row.add_widget(MDLabel(text=text, valign="middle"))
        sw = MDSwitch(active=bool(self.cfg.get(key, True)),
                      size_hint=(None, None), pos_hint={"center_y": 0.5})
        sw.bind(active=lambda inst, v: self.set_cfg(key, bool(v)))
        row.add_widget(sw)
        return row

    def goto(self, name):
        self.sm.current = name

    def refresh_info(self):
        if not hasattr(self, "swatches"):
            return
        c = self.cfg
        n = os.cpu_count() or 2
        dbt = pretty(c["db_dir"]) if c["db_dir"] else "Not Set - Tap To Choose"
        self.db_home_lbl.text = dbt
        self.db_lbl.text = dbt
        self.cpu_lbl.text = "Using %d Of %d Cores" % (self.workers(), n)
        hz = display_hz()
        self.hz_lbl.text = ("Screen Refresh Rate: %d Hz" % hz) if hz else ""
        for k, b in self.mode_btns.items():
            self.style_toggle(b, k == c.get("theme", "auto"))
        for name, b in self.swatches.items():
            b.icon = ("check-circle" if name == c.get("palette")
                      else "checkbox-blank-circle")
        self.refresh_battery()

    def refresh_battery(self):
        ok = battery_ok()
        self.batt_icon.icon = "check-circle" if ok else "alert-circle"
        self.batt_icon.text_color = ((0.3, 0.78, 0.4, 1) if ok
                                     else (1.0, 0.65, 0.2, 1))
        self.batt_lbl.text = ("Battery: Unrestricted" if ok
                              else "Battery: Restricted")
        self.batt_btn.opacity = 0 if ok else 1
        self.batt_btn.disabled = ok

    # ---- logging
    def add_log(self, msg):
        self.lines.append(msg)
        self.lines = self.lines[-400:]
        self.log_lbl.text = "\n".join(self.lines)

    def log(self, msg):
        Clock.schedule_once(lambda dt: self.add_log(msg))

    def set_status(self, msg, pct=None):
        now = time.time()
        if pct is not None and 0 < pct < 100:
            if now - self._last_ui < 0.12:
                return
        self._last_ui = now

        def f(dt):
            self.status_lbl.text = msg
            if pct is not None:
                Animation.cancel_all(self.bar, "value")
                Animation(value=pct, d=0.3, t="out_quad").start(self.bar)
        Clock.schedule_once(f)

    # ---- android lifecycle
    def on_start(self):
        Clock.schedule_once(lambda dt: ask_access(), 1)
        set_hi_refresh()
        try:
            from android import activity
            activity.bind(on_activity_result=self.on_result)
        except Exception:
            pass
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
        fade_in(self.home_cards)
        Clock.schedule_once(lambda dt: self.refresh_info(), 1.5)
        Clock.schedule_once(lambda dt: self.check_battery_popup(), 3)

    def check_battery_popup(self):
        if not battery_ok():
            self.battery_dialog()

    def on_pause(self):
        return True

    def on_resume(self):
        set_hi_refresh()
        if self.cfg.get("theme") == "auto":
            self.apply_theme()
        Clock.schedule_once(lambda dt: self.refresh_info(), 0.5)

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

    # ---- system picker (Android Files app)
    def system_pick(self, kind, cb):
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            if kind == "dir":
                it = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            else:
                it = Intent(Intent.ACTION_OPEN_DOCUMENT)
                it.addCategory(Intent.CATEGORY_OPENABLE)
                it.setType("*/*")
            self._sys_cb = (kind, cb)
            _act().startActivityForResult(it, 7001)
        except Exception:
            self.set_status("System Picker Is Not Available")

    def on_result(self, req, res, intent):
        if req != 7001 or not self._sys_cb:
            return
        kind, cb = self._sys_cb
        self._sys_cb = None
        if res != -1 or intent is None:
            return
        p = None
        try:
            from jnius import autoclass
            DC = autoclass("android.provider.DocumentsContract")
            uri = intent.getData()
            doc = (DC.getTreeDocumentId(uri) if kind == "dir"
                   else DC.getDocumentId(uri))
            p = docid_to_path(doc, kind == "dir")
        except Exception:
            p = None
        if p is None:
            Clock.schedule_once(lambda dt: self.set_status(
                "Could Not Use That Location. Use The Built-In Browser."))
        else:
            Clock.schedule_once(lambda dt: cb(p))

    # ---- dialogs
    def battery_dialog(self):
        dlg = None

        def allow(*_):
            request_battery_exemption()
            dlg.dismiss()

        def appset(*_):
            open_app_settings()
            dlg.dismiss()

        dlg = MDDialog(
            title="Keep Persist Running",
            text=("Android Can Stop Persist When The Screen Is Off.\n\n"
                  "1. Tap Allow Now And Choose Allow.\n"
                  "2. Tap App Settings, Open Battery Saver And Pick "
                  "No Restrictions.\n"
                  "3. Turn On Autostart.\n"
                  "4. Lock Persist In Recent Apps.\n\n"
                  "This Popup Only Appears While The Restriction Is Active."),
            buttons=[MDFlatButton(text="Later",
                                  on_release=lambda *_: dlg.dismiss()),
                     MDFlatButton(text="App Settings", on_release=appset),
                     MDFlatButton(text="Allow Now", on_release=allow)])
        dlg.open()

    def choose_folder(self, key, title):
        start = self.cfg.get(key) or ""
        Picker("dir", Path(start) if start else ROOT, title,
               lambda p: self.set_cfg(key, str(p))).open()

    # ---- flows
    def start_scan(self):
        if self.busy:
            self.set_status("A Job Is Already Running")
            return

        def go(db_dir):
            Picker("dir", ROOT, "Pick The Folder To Scan",
                   lambda p: self.run_job("Scan", do_scan, p, db_dir)).open()

        cur = self.cfg.get("db_dir") or ""
        if cur and Path(cur).is_dir():
            go(Path(cur))
            return

        def picked(p):
            self.set_cfg("db_dir", str(p))
            go(p)
        Picker("dir", ROOT, "Choose Where To Save Databases", picked).open()

    def start_extract(self):
        if self.busy:
            self.set_status("A Job Is Already Running")
            return

        def got_zip(z):
            dbd = self.cfg.get("db_dir")
            start = Path(dbd) if dbd and Path(dbd).is_dir() else ROOT

            def got_db(d):
                self.pick_output(z, d)
            Picker(".json", start, "Pick The Database", got_db).open()
        Picker(".zip", ROOT, "Pick The Takeout Zip", got_zip).open()

    def pick_output(self, z, d):
        last = self.cfg.get("last_out") or ""
        start = Path(last) if last and Path(last).is_dir() else ROOT

        def got_out(o):
            self.set_cfg("last_out", str(o))
            self.confirm_extract(z, d, o)
        Picker("dir", start, "Choose Output Folder For This Extraction",
               got_out).open()

    def confirm_extract(self, z, d, out):
        dlg = None

        def start(*_):
            dlg.dismiss()
            self.run_job("Extract", do_extract, z, d, out)

        def change(*_):
            dlg.dismiss()
            self.pick_output(z, d)

        dlg = MDDialog(
            title="Start Extraction?",
            text=("Zip: %s\nDatabase: %s\nOutput: %s\n\n"
                  "Nothing Existing Is Overwritten." %
                  (z.name, d.name, pretty(out))),
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
            lowprio()
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
