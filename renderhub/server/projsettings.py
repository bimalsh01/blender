"""Per-project choices made on this PC (never stored in the package, so packages stay portable).
Today: which devices render the project. state/project_devices.json"""

import threading
from pathlib import Path

from . import util

BACKENDS = ["auto", "OPTIX", "CUDA", "HIP", "METAL", "ONEAPI", "CPU"]


def validate(choice):
    if not isinstance(choice, dict):
        raise ValueError("device choice must be an object")
    backend = str(choice.get("backend") or "auto")
    backend = "auto" if backend.lower() == "auto" else backend.upper()
    if backend not in BACKENDS:
        raise ValueError("backend must be one of %s" % ", ".join(BACKENDS))
    devices = choice.get("devices") or []
    if not isinstance(devices, list) or len(devices) > 32 or not all(isinstance(d, str) and len(d) < 300 for d in devices):
        raise ValueError("devices must be a list of device ids")
    try:
        threads = int(choice.get("threads") or 0)
    except (TypeError, ValueError):
        raise ValueError("threads must be a whole number (0 = all)")
    if not 0 <= threads <= 1024:
        raise ValueError("threads must be between 0 (all) and 1024")
    return {"backend": backend, "devices": devices if backend not in ("auto", "CPU") else [],
            "use_cpu": bool(choice.get("use_cpu")) and backend != "CPU", "threads": threads}


class ProjectDevices:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "project_devices.json"
        self.lock = threading.RLock()
        self.data = util.read_json(self.path, {}) or {}

    def get(self, name):
        with self.lock:
            c = self.data.get(name)
            return dict(c) if c else None

    def set(self, name, choice):
        choice = validate(choice)
        with self.lock:
            if choice == {"backend": "auto", "devices": [], "use_cpu": False, "threads": 0}:
                self.data.pop(name, None)  # automatic = no entry
            else:
                self.data[name] = choice
            util.write_json_atomic(self.path, self.data)
        return choice

    def delete(self, name):
        with self.lock:
            if self.data.pop(name, None) is not None:
                util.write_json_atomic(self.path, self.data)
