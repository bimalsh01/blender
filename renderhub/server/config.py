"""config.json: load, validate, save. Folder paths are relative to the studio folder
(the folder that contains renderhub/)."""

import copy
import threading
from pathlib import Path

from . import util

RENDERHUB_DIR = Path(__file__).resolve().parent.parent
STUDIO_DIR = RENDERHUB_DIR.parent
CONFIG_PATH = RENDERHUB_DIR / "config.json"

DEVICE_CHOICES = ["auto", "OPTIX", "CUDA", "HIP", "METAL", "ONEAPI", "CPU"]

DEFAULTS = {
    "blender_path": "",
    "ffmpeg_path": "",
    "port": 8800,
    "max_concurrent_jobs": 1,
    "chunk_size": 0,
    "device_override": "auto",
    "vram_gb_override": None,
    "projects_dir": "projects",
    "output_dir": "output",
    "logs_dir": "logs",
    "state_dir": "state",
    "scan_interval_seconds": 4,
    "settle_seconds": 5,
    "prevent_sleep": True,
    "max_zip_extract_gb": 500,
}

# What each setting means, shown in the UI and the README.
HELP = {
    "blender_path": "Full path to Blender (blender.exe, Blender.app or the blender binary). Empty = find it automatically.",
    "ffmpeg_path": "Full path to ffmpeg, for the finished video (sound, captions, text). Empty = look on PATH and the usual install folders. Needs a build with drawtext (FreeType).",
    "port": "Port for the local page (127.0.0.1 only). Takes effect after a restart.",
    "max_concurrent_jobs": "How many Blender renders run at once. Keep 1 per GPU: two jobs on one GPU ran out of memory before.",
    "chunk_size": "Split shots longer than this many frames into several jobs. 0 = one job per shot.",
    "device_override": "auto = best available (OptiX, CUDA, HIP, Metal, oneAPI, then CPU). Pick one to force it.",
    "vram_gb_override": "GPU memory in GB, for GPUs whose VRAM cannot be read (for example AMD on Windows). Empty = detect.",
    "settle_seconds": "A dropped folder or zip must stop changing for this long before it is checked.",
    "scan_interval_seconds": "How often projects/ is scanned.",
    "prevent_sleep": "Keep the PC awake while jobs run.",
}


class ConfigError(ValueError):
    pass


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        data = util.read_json(self.path, None)
        if data is None and self.path.exists():
            raise ConfigError(
                "config.json is not valid JSON. Fix it or delete it to get the defaults: %s" % self.path
            )
        self.data = copy.deepcopy(DEFAULTS)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS:
                    self.data[k] = v
        try:
            self.data = self._validated(self.data)
        except ConfigError as e:
            raise ConfigError("config.json: %s" % e)
        if not self.path.exists():
            self.save()

    def get(self, key):
        with self.lock:
            return self.data.get(key, DEFAULTS.get(key))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, changes):
        """Apply a dict of changes after validation. Returns the list of changed keys."""
        with self.lock:
            new = copy.deepcopy(self.data)
            for k, v in changes.items():
                if k not in DEFAULTS:
                    raise ConfigError("unknown setting: %s" % k)
                new[k] = v
            new = self._validated(new)
            changed = [k for k in new if new[k] != self.data.get(k)]
            self.data = new
            self.save()
            return changed

    def save(self):
        with self.lock:
            util.write_json_atomic(self.path, self.data)

    def folder(self, key):
        """Resolve a *_dir setting to an absolute folder and make sure it exists."""
        p = Path(self.get(key)).expanduser()
        if not p.is_absolute():
            p = STUDIO_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _validated(d):
        def as_int(key, lo, hi):
            try:
                v = int(d[key])
            except (TypeError, ValueError):
                raise ConfigError("%s must be a whole number" % key)
            if not lo <= v <= hi:
                raise ConfigError("%s must be between %d and %d" % (key, lo, hi))
            d[key] = v

        as_int("port", 1024, 65535)
        as_int("max_concurrent_jobs", 1, 16)
        as_int("chunk_size", 0, 1000000)
        as_int("scan_interval_seconds", 1, 600)
        as_int("settle_seconds", 1, 600)
        as_int("max_zip_extract_gb", 1, 100000)
        dev = str(d.get("device_override") or "auto")
        dev = "auto" if dev.lower() == "auto" else dev.upper()
        if dev not in DEVICE_CHOICES:
            raise ConfigError("device_override must be one of %s" % ", ".join(DEVICE_CHOICES))
        d["device_override"] = dev
        v = d.get("vram_gb_override")
        if v in ("", None):
            d["vram_gb_override"] = None
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ConfigError("vram_gb_override must be a number of GB or empty")
            if not 0.5 <= v <= 1024:
                raise ConfigError("vram_gb_override must be between 0.5 and 1024 GB")
            d["vram_gb_override"] = v
        for key in ("blender_path", "ffmpeg_path", "projects_dir", "output_dir", "logs_dir", "state_dir"):
            d[key] = str(d.get(key) or "").strip().strip('"')
        for key in ("projects_dir", "output_dir", "logs_dir", "state_dir"):
            if not d[key]:
                d[key] = DEFAULTS[key]
        d["prevent_sleep"] = bool(d.get("prevent_sleep"))
        return d
