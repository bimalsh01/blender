"""RenderHub entry point.  Start with:  python run.py   (options: --port N, --no-browser)"""

import sys

if sys.version_info < (3, 10):
    print("RenderHub needs Python 3.10 or newer; this is %s." % sys.version.split()[0])
    print("Install Python from https://www.python.org/downloads/ and run again.")
    sys.exit(1)

import argparse
import errno
import os
import signal
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import VERSION, util  # noqa: E402
from server.api import App, make_server  # noqa: E402
from server.blender import BlenderLocator  # noqa: E402
from server.config import Config, ConfigError  # noqa: E402
from server.hardware import Hardware  # noqa: E402
from server.jobs import JobManager  # noqa: E402
from server.projsettings import ProjectDevices  # noqa: E402
from server.scanner import Scanner  # noqa: E402


def take_instance_lock(state_dir):
    """OS-level file lock: released automatically if RenderHub dies, so no stale locks."""
    path = Path(state_dir) / "renderhub.lock"
    f = open(path, "a+")
    try:
        if util.IS_WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def main():
    ap = argparse.ArgumentParser(description="RenderHub: render project packages on this PC.")
    ap.add_argument("--port", type=int, help="override the port in config.json")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()

    try:
        cfg = Config()
    except ConfigError as e:
        print("RenderHub cannot start: %s" % e)
        return 1
    state_dir = cfg.folder("state_dir")
    logs_dir = cfg.folder("logs_dir")
    util.set_log_path(logs_dir / "renderhub.log")
    port = args.port or cfg.get("port")
    url = "http://127.0.0.1:%d/" % port

    lock = take_instance_lock(state_dir)
    if lock is None:
        print("RenderHub is already running on this PC (another window holds %s)." % (state_dir / "renderhub.lock"))
        print("Use that one, or close it first. Its page is normally at http://127.0.0.1:%d/" % cfg.get("port"))
        return 2

    locator = BlenderLocator(cfg, state_dir)
    hw = Hardware(cfg, locator, state_dir)
    scanner = Scanner(cfg, hw, locator, state_dir)
    scanner.devices = ProjectDevices(state_dir)
    jobs = JobManager(cfg, hw, locator, scanner, state_dir)
    app = App(cfg, locator, hw, scanner, jobs)
    try:
        server = make_server(app, port)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048, 10013):
            print("Port %d is already in use, so RenderHub cannot start." % port)
            print("Is RenderHub already open? Try %s . Otherwise change \"port\" in renderhub/config.json "
                  "or run: python run.py --port 8801" % url)
        else:
            print("RenderHub cannot listen on 127.0.0.1:%d: %s" % (port, e))
        return 2

    util.log("RenderHub %s starting. Studio folder: %s" % (VERSION, cfg.folder("projects_dir").parent))
    info = locator.refresh()
    if info["selected"]:
        util.log("Blender %s at %s" % (info["selected"]["version"], info["selected"]["path"]))
    else:
        util.log("Blender not found: %s" % info["error"])
    if not hw.probe_is_current():
        hw.redetect(background=True)
    scanner.start()
    jobs.start()

    def on_term(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_term)
            except (ValueError, OSError):
                pass

    util.log("Open %s  (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        util.log("Stopping RenderHub.")
    finally:
        jobs.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
