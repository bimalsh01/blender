"""Find Blender, read its version, and run RenderHub's scripts inside it."""

import glob
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from . import util
from .config import RENDERHUB_DIR

SCRIPTS_DIR = RENDERHUB_DIR / "blender_scripts"
JSON_MARKER = "RENDERHUB_JSON:"
VERSION_RE = re.compile(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?")


def resolve_executable(path):
    """Accept Blender.app, a folder that contains Blender, or the binary itself."""
    if not path:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"'))))
    if p.suffix.lower() == ".app" or (p.is_dir() and (p / "Contents" / "MacOS").is_dir()):
        p = p / "Contents" / "MacOS" / "Blender"
    elif p.is_dir():
        for name in ("blender.exe", "blender", "Blender"):
            if (p / name).is_file():
                p = p / name
                break
    if p.is_file() and os.access(str(p), os.X_OK):
        return str(p)
    return None


def standard_locations():
    found = []
    if util.IS_WINDOWS:
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        for root in roots:
            if not root:
                continue
            found += glob.glob(os.path.join(root, "Blender Foundation", "*", "blender.exe"))
            found += glob.glob(os.path.join(root, "Programs", "Blender Foundation", "*", "blender.exe"))
            found += glob.glob(os.path.join(root, "Steam", "steamapps", "common", "Blender", "blender.exe"))
    elif util.IS_MAC:
        for root in ("/Applications", os.path.expanduser("~/Applications")):
            found += glob.glob(os.path.join(root, "Blender*.app"))
            found += glob.glob(os.path.join(root, "*", "Blender*.app"))
    else:
        found += ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
                  "/var/lib/flatpak/exports/bin/org.blender.Blender",
                  os.path.expanduser("~/.local/share/flatpak/exports/bin/org.blender.Blender")]
        found += glob.glob("/opt/blender*/blender") + glob.glob(os.path.expanduser("~/blender*/blender"))
        found += glob.glob(os.path.expanduser("~/Applications/blender*/blender"))
    return found


class BlenderLocator:
    """Keeps the list of Blender installs and the one in use."""

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        self.cache_path = Path(state_dir) / "blender_versions.json"
        self.version_cache = util.read_json(self.cache_path, {}) or {}
        self.lock = threading.RLock()
        self._info = None

    def version_of(self, exe):
        try:
            st = os.stat(exe)
            key = "%s|%d|%d" % (exe, st.st_size, int(st.st_mtime))
        except OSError:
            return None
        with self.lock:
            if key in self.version_cache:
                return self.version_cache[key]
        rc, out = util.run([exe, "--version"], timeout=60)
        m = VERSION_RE.search(out or "")
        if not m:
            return None
        info = {"string": "%s.%s.%s" % (m.group(1), m.group(2), m.group(3) or "0"),
                "tuple": [int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)]}
        with self.lock:
            self.version_cache[key] = info
            try:
                util.write_json_atomic(self.cache_path, self.version_cache)
            except OSError:
                pass
        return info

    def refresh(self):
        configured = self.cfg.get("blender_path")
        candidates = []
        seen = set()

        def add(path, source):
            exe = resolve_executable(path)
            if not exe:
                return
            key = os.path.normcase(os.path.realpath(exe))
            if key in seen:
                return
            seen.add(key)
            v = self.version_of(exe)
            candidates.append({"path": exe, "source": source,
                               "version": v["string"] if v else None,
                               "version_tuple": v["tuple"] if v else None})

        if configured:
            add(configured, "config.json")
        on_path = shutil.which("blender") or shutil.which("Blender")
        if on_path:
            add(on_path, "PATH")
        for p in standard_locations():
            add(p, "standard folder")

        info = {"candidates": candidates, "selected": None, "error": None, "notice": None}
        usable = [c for c in candidates if c["version"]]
        if configured:
            exe = resolve_executable(configured)
            sel = next((c for c in candidates if exe and c["path"] == exe), None)
            if sel and sel["version"]:
                info["selected"] = sel
            elif sel:
                info["error"] = ("The Blender set in Settings does not answer to --version: %s" % configured)
            else:
                info["error"] = ("The Blender path in Settings does not exist or is not a program: %s. "
                                 "Fix it in Settings or clear it to search automatically." % configured)
        elif usable:
            usable.sort(key=lambda c: c["version_tuple"], reverse=True)
            info["selected"] = usable[0]
            if len(usable) > 1:
                info["notice"] = ("%d Blender installs found; using the newest (%s). Pick another in Settings."
                                  % (len(usable), usable[0]["version"]))
        if not info["selected"] and not info["error"]:
            if util.IS_WINDOWS:
                hint = r"for example C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
            elif util.IS_MAC:
                hint = "for example /Applications/Blender.app"
            else:
                hint = "for example /usr/bin/blender"
            info["error"] = ("Blender was not found (looked in config.json, PATH and the standard install folders). "
                             "Paste the path to Blender in Settings, %s." % hint)
        with self.lock:
            self._info = info
        return info

    def info(self):
        with self.lock:
            if self._info is None:
                return self.refresh()
            return self._info

    def selected(self):
        return self.info().get("selected")


def run_script(blender_exe, script_name, args, state_dir, blend_file=None, timeout=600,
               autoexec=False, log_path=None):
    """Run blender_scripts/<script_name> headless. Arguments go through a JSON file so paths
    with spaces or non-ASCII characters are safe. Returns dict:
    {ok, result (parsed JSON or None), returncode, output_tail, error}."""
    tmp_dir = Path(state_dir) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    args_file = tmp_dir / ("args-%s.json" % uuid.uuid4().hex[:12])
    util.write_json_atomic(args_file, args)
    cmd = [blender_exe, "-b"]
    cmd += ["--factory-startup", "-noaudio"]
    cmd += ["--enable-autoexec"] if autoexec else ["--disable-autoexec"]
    if blend_file:
        cmd.append(str(blend_file))
    cmd += ["--python-exit-code", "3", "--python", str(SCRIPTS_DIR / script_name), "--", str(args_file)]
    try:
        rc, out = util.run(cmd, timeout=timeout)
    finally:
        try:
            args_file.unlink()
        except OSError:
            pass
    if log_path:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(" ".join('"%s"' % c for c in cmd) + "\n\n" + (out or ""))
        except OSError:
            pass
    result = None
    for line in (out or "").splitlines():
        if line.startswith(JSON_MARKER):
            try:
                result = json.loads(line[len(JSON_MARKER):])
            except ValueError:
                pass
    res = {"ok": rc == 0 and result is not None, "result": result, "returncode": rc,
           "output_tail": util.tail(out, 25), "error": None}
    if rc is None:
        res["error"] = "Blender did not finish: %s" % util.tail(out, 3)
    elif result is None:
        res["error"] = "Blender exited with code %s without a report. Last lines:\n%s" % (rc, util.tail(out, 8))
    elif rc != 0:
        res["error"] = "Blender exited with code %s. Last lines:\n%s" % (rc, util.tail(out, 8))
    return res
