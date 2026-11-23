"""Small helpers shared by the server modules."""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

_log_lock = threading.Lock()
_log_path = None


def set_log_path(path):
    global _log_path
    _log_path = Path(path)


def log(msg):
    """Write a line to the console and to logs/renderhub.log."""
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        if _log_path:
            try:
                with open(_log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass


def log_exception(context):
    log("ERROR in %s:\n%s" % (context, traceback.format_exc()))


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def longpath(path):
    """On Windows, return a \\\\?\\ path so paths over 260 chars still work."""
    p = str(path)
    if IS_WINDOWS and not p.startswith("\\\\?\\"):
        p = os.path.abspath(p)
        if len(p) > 240:
            if p.startswith("\\\\"):
                return "\\\\?\\UNC\\" + p[2:]
            return "\\\\?\\" + p
    return p


def read_json(path, default=None):
    try:
        with open(longpath(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    """Write JSON to a temp file, then rename, so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_file(path, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(longpath(path), "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human_bytes(n):
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def gb(n_bytes):
    return None if n_bytes is None else n_bytes / (1024.0 ** 3)


def run(cmd, timeout=60, cwd=None):
    """Run a command, return (returncode, combined output). Never raises for
    missing programs or timeouts; returncode is None in those cases."""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            cwd=cwd,
            **kwargs
        )
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None, "program not found: %s" % cmd[0]
    except PermissionError:
        return None, "not allowed to run: %s" % cmd[0]
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode("utf-8", errors="replace")
        return None, out + "\n[timed out after %ss]" % timeout
    except OSError as e:
        return None, "could not run %s: %s" % (cmd[0], e)


def tail(text, lines=20):
    parts = (text or "").strip().splitlines()
    return "\n".join(parts[-lines:])
