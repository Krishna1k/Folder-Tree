import re, json, time, shutil, zipfile, hashlib, threading
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

ROOT = Path("/storage/emulated/0")
BK = ROOT / "PhotoMap"


def ask_access():
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([Permission.READ_EXTERNAL_STORAGE,
                             Permission.WRITE_EXTERNAL_STORAGE])
    except Exception:
        pass
    try:
        from jnius import autoclass
        Env = autoclass("android.os.Environment")
        if Env.isExternalStorageManager():
            return
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        i = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        i.setData(Uri.parse("package:" + act.getPackageName()))
        act.startActivity(i)
    except Exception:
        pass


def sha_stream(f):
    h = hashlib.sha256()
    for c in iter(lambda: f.read(1 << 20), b""):
        h.update(c)
    return h.hexdigest()


def safe(dest, rel):
    t = (dest / rel).resolve()
    return t if dest.resolve() in t.parents else None


def do_scan(root, log, status):
    files = [p for p in root.rglob("*") if p.is_file()]
    log("Scanning %d files in %s" % (len(files), root.name))
    items = []
    for n, p in enumerate(files, 1):
        try:
            with open(p, "rb") as f:
                h = sha_stream(f)
            st = p.stat()
            items.append({"path": p.relative_to(root).as_posix(),
                          "hash": h, "size": st.st_size,
                          "mtime": st.st_mtime})
        except Exception as e:
            log("Skipped %s (%r)" % (p.name, e))
        if n % 20 == 0:
            status("Scanning %d / %d" % (n, len(files)))
    BK.mkdir(exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", root.name)
    out = BK / ("manifest_%s_%s.json" % (name, time.strftime("%Y%m%d_%H%M%S")))
    out.write_text(json.dumps(items))
    log("DONE. %d files saved in database:" % len(items))
    log("PhotoMap/" + out.name)
    log("Back This File Up (Drive).")


def do_extract(zpath, mpath, log, status):
    log("Checking zip: " + zpath.name)
    try:
        zf = zipfile.ZipFile(zpath)
        bad = zf.testzip()
    except Exception as e:
        log("ZIP IS DAMAGED: %r" % e)
        return
    if bad:
        log("ZIP IS DAMAGED at: " + bad)
        return
    items = json.loads(mpath.read_text())
    by_hash, by_ns = {}, {}
    for it in items:
        by_hash.setdefault(it["hash"], []).append(it)
        k = (Path(it["path"]).name, it["size"])
        by_ns.setdefault(k, []).append(it)
    dest = ROOT / ("Restored_" + mpath.stem.replace("manifest_", ""))
    dest.mkdir(exist_ok=True)
    tmp = dest / ".tmp_photomap"
    infos = [i for i in zf.infolist()
             if not i.is_dir() and not i.filename.lower().endswith(".json")]
    ok = lost = 0
    for n, i in enumerate(infos, 1):
        h = hashlib.sha256()
        with zf.open(i) as f, open(tmp, "wb") as o:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
                o.write(c)
        hits = by_hash.get(h.hexdigest()) or by_ns.get(
            (Path(i.filename).name, i.file_size))
        if hits:
            ok += 1
            targets = [(x["path"], x.get("mtime")) for x in hits]
        else:
            lost += 1
            targets = [("_Unmatched/" + i.filename, None)]
        first = None
        for rel, mt in targets:
            t = safe(dest, rel)
            if t is None or t.exists():
                continue
            t.parent.mkdir(parents=True, exist_ok=True)
            if first is None:
                shutil.move(str(tmp), str(t))
                first = t
            else:
                shutil.copy2(first, t)
            if mt:
                try:
                    import os
                    os.utime(t, (mt, mt))
                except Exception:
                    pass
        if first is None and tmp.exists():
            tmp.unlink()
        if n % 20 == 0:
            status("Extracting %d / %d" % (n, len(infos)))
    missing = [it["path"] for it in items if not (dest / it["path"]).exists()]
    (BK / ("missing_%s.txt" % mpath.stem)).write_text("\n".join(missing))
    log("DONE. In Original Folders: %d | In _Unmatched: %d" % (ok, lost))
    log("Still Missing From Database: %d" % len(missing))
    log("(Normal If More Takeout Zips Are Left.)")
    log("Output: " + dest.name)


class Picker(Popup):
    def __init__(self, mode, start, on_pick):
        titles = {"dir": "Pick A Folder", ".zip": "Pick A Zip",
                  ".json": "Pick A Database"}
        body = BoxLayout(orientation="vertical", spacing=dp(6))
        path = Label(size_hint_y=None, height=dp(40), font_size="12sp")
        grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(4))
        grid.bind(minimum_height=grid.setter("height"))
        sv = ScrollView()
        sv.add_widget(grid)
        bar = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        up = Button(text="Up")
        up.bind(on_release=lambda *_: self.go_up())
        bar.add_widget(up)
        if mode == "dir":
            use = Button(text="Use This Folder")
            use.bind(on_release=lambda *_: self.choose(self.cur))
            bar.add_widget(use)
        cancel = Button(text="Cancel")
        cancel.bind(on_release=lambda *_: self.dismiss())
        bar.add_widget(cancel)
        body.add_widget(path)
        body.add_widget(sv)
        body.add_widget(bar)
        super().__init__(title=titles[mode], content=body,
                         size_hint=(0.95, 0.95))
        self.mode, self.cur, self.on_pick = mode, start, on_pick
        self.path_lbl, self.grid = path, grid
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

    def refresh(self):
        self.path_lbl.text = str(self.cur)
        self.grid.clear_widgets()
        try:
            ents = sorted(self.cur.iterdir(),
                          key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            ents = []
        for p in ents:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                b = Button(text="[DIR]  " + p.name)
                b.bind(on_release=lambda _, p=p: self.go(p))
            elif self.mode != "dir" and p.suffix.lower() == self.mode:
                mb = p.stat().st_size // 1048576
                b = Button(text="%s  (%d MB)" % (p.name, mb))
                b.bind(on_release=lambda _, p=p: self.choose(p))
            else:
                continue
            b.size_hint_y = None
            b.height = dp(52)
            self.grid.add_widget(b)


class PhotoMapApp(App):
    def build(self):
        self.busy = False
        self.lines = []
        root = BoxLayout(orientation="vertical", padding=dp(10),
                         spacing=dp(8))
        root.add_widget(Label(text="PhotoMap", font_size="24sp",
                              size_hint_y=None, height=dp(44)))
        b1 = Button(text="1. Scan A Folder (Before Upload)",
                    size_hint_y=None, height=dp(64))
        b1.bind(on_release=lambda *_: self.ask_scan())
        b2 = Button(text="2. Extract A Zip (After Takeout)",
                    size_hint_y=None, height=dp(64))
        b2.bind(on_release=lambda *_: self.ask_extract())
        self.status_lbl = Label(text="Ready", size_hint_y=None,
                                height=dp(30))
        sv = ScrollView()
        self.log_lbl = Label(text="", size_hint_y=None, halign="left",
                             valign="top", font_size="14sp")
        self.log_lbl.bind(
            width=lambda *a: setattr(self.log_lbl, "text_size",
                                     (self.log_lbl.width, None)),
            texture_size=lambda *a: setattr(self.log_lbl, "height",
                                            self.log_lbl.texture_size[1]))
        sv.add_widget(self.log_lbl)
        for w in (b1, b2, self.status_lbl, sv):
            root.add_widget(w)
        return root

    def on_start(self):
        ask_access()
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            WM = autoclass("android.view.WindowManager$LayoutParams")

            @run_on_ui_thread
            def keep_on():
                act.getWindow().addFlags(WM.FLAG_KEEP_SCREEN_ON)
            keep_on()
        except Exception:
            pass

    def log(self, msg):
        def f(dt):
            self.lines.append(msg)
            self.log_lbl.text = "\n".join(self.lines[-200:])
        Clock.schedule_once(f)

    def set_status(self, msg):
        Clock.schedule_once(lambda dt: setattr(self.status_lbl, "text", msg))

    def ask_scan(self):
        if not self.busy:
            Picker("dir", ROOT, lambda p: self.run(do_scan, p)).open()

    def ask_extract(self):
        if self.busy:
            return
        Picker(".zip", ROOT, self.pick_manifest).open()

    def pick_manifest(self, z):
        if not BK.exists() or not list(BK.glob("manifest_*.json")):
            self.log("No database yet. Scan a folder first.")
            return
        Picker(".json", BK,
               lambda m: self.run(do_extract, z, m)).open()

    def run(self, fn, *args):
        self.busy = True

        def work():
            try:
                fn(*args, self.log, self.set_status)
            except Exception as e:
                self.log("ERROR: %r" % e)
            finally:
                self.busy = False
                self.set_status("Ready")
        threading.Thread(target=work, daemon=True).start()


PhotoMapApp().run()
