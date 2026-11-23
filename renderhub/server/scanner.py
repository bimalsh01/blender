"""Watches projects/: settle check, safe zip import, package validation, and the
Blender-side check. Never deletes or edits a package (only extracts zips)."""

import os
import queue
import shutil
import threading
import time
from pathlib import Path

from . import assemble
from . import blender as blender_mod
from . import package, requirements, util

FRESH_SECONDS = 30  # on first sight, files untouched this long are taken as settled


def signature(path, kind):
    """(file count, total bytes, newest mtime_ns): changes while a copy is in progress."""
    if kind == "zip":
        try:
            st = os.stat(util.longpath(path))
            return (1, st.st_size, st.st_mtime_ns)
        except OSError:
            return None
    count = total = newest = 0
    for root, dirs, files in os.walk(util.longpath(path)):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            try:
                st = os.stat(os.path.join(root, fn))
            except OSError:
                continue
            count += 1
            total += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return (count, total, newest)


class Scanner:
    def __init__(self, cfg, hw, locator, state_dir):
        self.cfg = cfg
        self.hw = hw
        self.locator = locator
        self.state_dir = Path(state_dir)
        self.lock = threading.RLock()
        self.entries = {}
        self.pkg_q = queue.Queue()
        self.check_q = queue.Queue()
        self.wake = threading.Event()
        self.hashes = package.HashCache(self.state_dir / "hashes.json")
        self.imports_path = self.state_dir / "imports.json"
        self.imports = util.read_json(self.imports_path, {}) or {}
        self.checks_path = self.state_dir / "checks.json"
        self.checks = util.read_json(self.checks_path, {}) or {}
        self.uploads_path = self.state_dir / "uploads.json"
        self.uploaded = set(util.read_json(self.uploads_path, []) or [])
        self.devices = None  # ProjectDevices, set by run.py
        self.just_extracted = set()  # folders RenderHub itself just wrote: complete, no settle wait
        self.last_scan = None
        self.scan_error = None

    # ------------------------------------------------------------ threads
    def start(self):
        pdir = self.cfg.folder("projects_dir")
        for leftover in pdir.glob(".renderhub-extract-*"):
            shutil.rmtree(util.longpath(leftover), ignore_errors=True)  # our own temp, never a package
        for leftover in pdir.glob(".upload-*.part"):
            try:
                leftover.unlink()  # an upload that never finished
            except OSError:
                pass
        for target, name in ((self._scan_loop, "scan"), (self._pkg_loop, "validate"), (self._check_loop, "blender-check")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def rescan(self):
        self.wake.set()

    def register_upload(self, zip_name):
        """A zip that arrived through the page is complete: check it at once, and remove
        RenderHub's copy after it has been extracted (the original stays on the user's PC)."""
        with self.lock:
            self.uploaded.add(zip_name)
            util.write_json_atomic(self.uploads_path, sorted(self.uploaded))
        self.wake.set()

    def _scan_loop(self):
        while True:
            try:
                self.scan_once()
                self.scan_error = None
            except Exception as e:
                self.scan_error = "Scanning projects/ failed: %s" % e
                util.log_exception("scan")
            with self.lock:
                settling = any(e["phase"] in ("waiting", "copying") for e in self.entries.values())
            # Scan every second while a copy is settling so a dropped package shows up quickly.
            self.wake.wait(1 if settling else self.cfg.get("scan_interval_seconds"))
            self.wake.clear()

    def _pkg_loop(self):
        while True:
            name, gen = self.pkg_q.get()
            try:
                self._process(name, gen)
            except Exception as e:
                util.log_exception("validate %s" % name)
                with self.lock:
                    e2 = self.entries.get(name)
                    if e2 and e2["gen"] == gen:
                        e2["phase"] = "done"
                        e2["package"] = {"manifest": None, "shots": [], "invalid": True, "total_frames": 0,
                                         "rules": [package.rule("package", "Package structure", "fail",
                                                                "Checking failed unexpectedly: %s" % e,
                                                                "See logs/renderhub.log.")]}
                        e2["validated_sig"] = e2["sig"]

    def _check_loop(self):
        while True:
            name = self.check_q.get()
            try:
                self._blender_check(name)
            except Exception:
                util.log_exception("blender check %s" % name)

    # ------------------------------------------------------------ scanning
    def scan_once(self):
        pdir = self.cfg.folder("projects_dir")
        settle = self.cfg.get("settle_seconds")
        now = time.monotonic()
        seen = set()
        for ch in sorted(os.scandir(util.longpath(pdir)), key=lambda c: c.name.lower()):
            name = ch.name
            if name.startswith(".") or name.startswith("~$"):
                continue
            if ch.is_dir():
                kind = "folder"
            elif ch.is_file() and name.lower().endswith(".zip"):
                kind = "zip"
            else:
                continue
            seen.add(name)
            sig = signature(ch.path, kind)
            with self.lock:
                e = self.entries.get(name)
                if e is None or e["kind"] != kind:
                    e = {"id": name, "kind": kind, "path": str(pdir / name), "sig": None, "sig_since": now,
                         "ever_changed": False, "phase": "waiting", "gen": 0, "validated_sig": None,
                         "package": None, "blender_check": None, "zip_problem": None, "zip_target": None,
                         "checked_at": None}
                    self.entries[name] = e
                    if (sig and sig[2] and (time.time() - sig[2] / 1e9) > FRESH_SECONDS) or name in self.uploaded \
                            or name in self.just_extracted:
                        self.just_extracted.discard(name)
                        e["sig_since"] = now - settle  # old files and finished uploads: no need to wait
                if sig != e["sig"]:
                    if e["sig"] is not None:
                        e["ever_changed"] = True
                        e["sig_since"] = now
                    e["sig"] = sig
                    e["gen"] += 1
                    if e["ever_changed"]:
                        e["phase"] = "copying"
                settled = now - e["sig_since"] >= settle
                busy = e["phase"] in ("queued", "validating", "extracting")
                if settled and e["validated_sig"] != sig and not busy:
                    e["phase"] = "queued"
                    self.pkg_q.put((name, e["gen"]))
        with self.lock:
            for name in [n for n in self.entries if n not in seen]:
                del self.entries[name]
        self.last_scan = util.now_iso()

    def _process(self, name, gen):
        with self.lock:
            e = self.entries.get(name)
            if not e or e["gen"] != gen:
                return
            e["phase"] = "extracting" if e["kind"] == "zip" else "validating"
            sig = e["sig"]
        if e["kind"] == "zip":
            self._import_zip(e, gen, sig)
            return
        pkg = package.validate_package(e["path"], self.hashes)
        self.hashes.save()
        with self.lock:
            if e["gen"] != gen:
                return  # changed while we were checking; the scan loop will queue it again
            e["package"] = pkg
            e["validated_sig"] = sig
            e["checked_at"] = util.now_iso()
            e["phase"] = "done"
            e["from_zip"] = next((z for z, rec in self.imports.items() if rec.get("target") == name), None)
            e["blender_check"] = None
        if not pkg["invalid"]:
            self._use_cached_check_or_queue(name)
        util.log("Checked %s: %s" % (name, "invalid" if pkg["invalid"] else "package ok"))

    # ------------------------------------------------------------ zips
    def _import_zip(self, e, gen, sig):
        zpath = Path(e["path"])
        pdir = self.cfg.folder("projects_dir")
        problem, target_name = None, None
        try:
            st = os.stat(util.longpath(zpath))
            rec = self.imports.get(zpath.name)
            if rec and (pdir / rec["target"]).exists():
                target_name = rec["target"]
                if rec["size"] != st.st_size or rec["mtime_ns"] != st.st_mtime_ns:
                    problem = ("A project named %s is already in RenderHub, so this zip was not added. To replace it, "
                               "press Remove on that project, then add the zip again (nothing is ever overwritten)."
                               % target_name)
            else:
                name, prefix, total = package.inspect_zip(zpath, self.cfg.get("max_zip_extract_gb") * 1024 ** 3)
                target_name = name
                target = pdir / name
                free = shutil.disk_usage(str(pdir)).free
                if target.exists():
                    problem = ("A project named %s is already in RenderHub, so this zip was not added. To replace it, "
                               "press Remove on that project, then add the zip again (nothing is ever overwritten)." % name)
                elif free < total + 1024 ** 3:
                    problem = ("Not enough free space to extract: needs %s, %s free."
                               % (util.human_bytes(total), util.human_bytes(free)))
                else:
                    util.log("Extracting %s -> projects/%s (%s)" % (zpath.name, name, util.human_bytes(total)))
                    with self.lock:
                        self.just_extracted.add(name)
                    package.extract_zip(zpath, prefix, target, pdir / (".renderhub-extract-" + name))
                    uploaded = zpath.name in self.uploaded
                    self.imports[zpath.name] = {"target": name, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                                "extracted_at": util.now_iso(), "uploaded": uploaded}
                    util.write_json_atomic(self.imports_path, self.imports)
                    if uploaded:
                        with self.lock:
                            self.uploaded.discard(zpath.name)
                            util.write_json_atomic(self.uploads_path, sorted(self.uploaded))
                        try:
                            os.unlink(util.longpath(zpath))  # RenderHub's own upload copy
                        except OSError:
                            pass
                    self.wake.set()
        except package.ZipProblem as p:
            problem = str(p)
        except OSError as ex:
            problem = "Extracting failed: %s" % ex
        with self.lock:
            if e["gen"] != gen:
                return
            e["zip_problem"] = problem
            e["zip_target"] = target_name
            e["validated_sig"] = sig
            e["phase"] = "done" if problem else "imported"
            e["checked_at"] = util.now_iso()
        if problem:
            util.log("Zip %s: %s" % (zpath.name, problem))

    # ------------------------------------------------------------ Blender check
    def _check_key(self, e):
        sel = self.locator.selected()
        if not sel:
            return None
        return "%s|%s|%s" % (list(e["sig"] or []), sel["path"], sel["version"])

    def _use_cached_check_or_queue(self, name):
        with self.lock:
            e = self.entries.get(name)
            if not e:
                return
            key = self._check_key(e)
            cached = self.checks.get(name)
            if key and cached and cached.get("key") == key:
                e["blender_check"] = cached
                return
            e["blender_check"] = None
        if key:
            self.check_q.put(name)

    def entry(self, name):
        with self.lock:
            e = self.entries.get(name)
            return dict(e) if e else None

    def source_zips(self, name):
        """Zip files in projects/ that this project folder was extracted from."""
        pdir = self.cfg.folder("projects_dir")
        with self.lock:
            return [pdir / z for z, rec in self.imports.items() if rec.get("target") == name and (pdir / z).exists()]

    def forget(self, name):
        """A project was removed by the user: drop everything remembered about it, so the
        same name can be uploaded again."""
        with self.lock:
            self.entries.pop(name, None)
            self.checks.pop(name, None)
            util.write_json_atomic(self.checks_path, self.checks)
            for z in [z for z, rec in self.imports.items() if rec.get("target") == name or z == name]:
                del self.imports[z]
                self.entries.pop(z, None)
            util.write_json_atomic(self.imports_path, self.imports)
            self.uploaded.discard(name)
        self.wake.set()

    def recheck(self, name=None):
        """Forget cached Blender checks (one project or all) and run them again."""
        with self.lock:
            names = [name] if name else [n for n, e in self.entries.items() if e["kind"] == "folder"]
            for n in names:
                self.checks.pop(n, None)
                e = self.entries.get(n)
                if e:
                    e["validated_sig"] = None  # full re-validation, including checksums
                    e["blender_check"] = None
        self.wake.set()

    def blender_changed(self):
        with self.lock:
            names = [n for n, e in self.entries.items() if e.get("package") and not e["package"]["invalid"]]
        for n in names:
            self._use_cached_check_or_queue(n)

    def _blender_check(self, name):
        with self.lock:
            e = self.entries.get(name)
            if not e or not e.get("package") or e["package"]["invalid"]:
                return
            key = self._check_key(e)
            if not key:
                return
            cached = self.checks.get(name)
            if cached and cached.get("key") == key:
                e["blender_check"] = cached
                return
            m = e["package"]["manifest"]
            folder = e["path"]
        fixes = sorted(set(m["global_fixes"] + [f for s in m["shots"] for f in s["fixes"]]))
        args = {"package": folder,
                "fixes": [str(package.fix_path(folder, f).relative_to(folder)).replace("\\", "/") for f in fixes],
                "shots": [{"id": s["id"], "scene_name": s["scene_name"], "camera": s["camera"]} for s in m["shots"]]}
        sel = self.locator.selected()
        util.log("Blender check of %s started" % name)
        started = time.time()
        res = blender_mod.run_script(sel["path"], "check_project.py", args, self.state_dir,
                                     blend_file=package.pkg_path(folder, m["blend_file"]), timeout=1800,
                                     log_path=self.cfg.folder("logs_dir") / ("check-%s.log" % name))
        rec = {"key": key, "at": util.now_iso(), "seconds": round(time.time() - started, 1)}
        if res["result"] is not None:
            rec.update(state="done", result=res["result"])
        else:
            rec.update(state="error", error=res["error"])
        with self.lock:
            self.checks[name] = rec
            util.write_json_atomic(self.checks_path, self.checks)
            e = self.entries.get(name)
            if e and self._check_key(e) == key:
                e["blender_check"] = rec
        util.log("Blender check of %s done in %.1fs (%s)" % (name, rec["seconds"], rec["state"]))

    # ------------------------------------------------------------ views
    def _context(self):
        hw = dict(self.hw.summary())
        hw["ffmpeg"] = assemble.find_ffmpeg(self.cfg.get("ffmpeg_path"))
        return hw, self.locator.info()

    def _row(self, e, hw, blender):
        choice = self.devices.get(e["id"]) if self.devices else None
        if choice:
            ff = hw.get("ffmpeg")
            hw = dict(self.hw.summary(choice), ffmpeg=ff)  # this project's own device choice
        rules = requirements.evaluate(e, hw, blender)
        status, text = requirements.overall(e, rules)
        pkg = e.get("package") or {}
        m = pkg.get("manifest") or {}
        problems = [r for r in rules if r["status"] in ("fail", "warn")]
        first = None
        if e.get("zip_problem"):
            first = e["zip_problem"]
        elif problems:
            first = sorted(problems, key=lambda r: r["status"] != "fail")[0]["message"]
        return {"id": e["id"], "kind": e["kind"], "title": m.get("title") or e["id"], "status": status,
                "status_text": text, "shots": len(pkg.get("shots") or []),
                "total_frames": pkg.get("total_frames") or 0, "first_problem": first,
                "fails": sum(1 for r in rules if r["status"] == "fail"),
                "warns": sum(1 for r in rules if r["status"] == "warn"),
                "from_zip": e.get("from_zip"), "zip_target": e.get("zip_target"), "phase": e["phase"],
                "device_label": hw["chosen_label"] or "no device", "device_custom": bool(choice)}, rules

    def list(self):
        hw, blender = self._context()
        with self.lock:
            entries = list(self.entries.values())
        rows = []
        for e in entries:
            if e["kind"] == "zip" and e["phase"] == "imported":
                continue  # its extracted folder is listed instead
            rows.append(self._row(e, hw, blender)[0])
        return rows

    def manifest(self, name):
        with self.lock:
            e = self.entries.get(name)
            pkg = (e or {}).get("package") or {}
            return pkg.get("manifest")

    def render_spec(self, name):
        """What the job runner needs: the full manifest, planned shots and current status."""
        hw, blender = self._context()
        with self.lock:
            e = self.entries.get(name)
            if not e or e["kind"] != "folder" or not e.get("package") or not e["package"].get("manifest"):
                return None
        row, rules = self._row(e, hw, blender)
        pkg = e["package"]
        return {"id": name, "path": e["path"], "status": row["status"], "status_text": row["status_text"],
                "problems": [r for r in rules if r["status"] == "fail"], "manifest": pkg["manifest"],
                "shots": pkg["shots"], "sig": e["sig"]}

    def detail(self, name):
        hw, blender = self._context()
        with self.lock:
            e = self.entries.get(name)
            if not e:
                return None
        row, rules = self._row(e, hw, blender)
        pkg = e.get("package") or {}
        m = pkg.get("manifest") or {}
        bc = e.get("blender_check") or {}
        return {
            "row": row,
            "path": e["path"],
            "rules": rules,
            "zip_problem": e.get("zip_problem"),
            "checked_at": e.get("checked_at"),
            "blender_check_at": bc.get("at"),
            "blender_check_seconds": bc.get("seconds"),
            "manifest": {k: m.get(k) for k in ("name", "title", "blend_file", "blender", "render", "requirements",
                                               "global_fixes", "created")} if m else None,
            "shots": pkg.get("shots") or [],
            "estimate": None,  # M2: from test renders
        }
