"""What this PC has: Cycles devices (asked from Blender), CPU, RAM, VRAM, disks.

VRAM is not always readable. Order: user override in Settings, nvidia-smi (NVIDIA),
unified memory (Apple Silicon), Windows registry (AMD/Intel, best effort), Linux sysfs (AMD).
If none works the answer is "unknown" and requirements downgrade VRAM checks to warnings.
"""

import ctypes
import os
import platform
import plistlib
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from . import blender as blender_mod
from . import util

BACKEND_PRIORITY = ["OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"]
PROBE_VERSION = 2
BACKEND_LABEL = {"OPTIX": "NVIDIA OptiX", "CUDA": "NVIDIA CUDA", "HIP": "AMD HIP",
                 "METAL": "Apple Metal", "ONEAPI": "Intel oneAPI", "CPU": "CPU"}

try:  # optional, never required
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


# ---------------------------------------------------------------- OS facts

def os_name():
    if util.IS_WINDOWS:
        ver = platform.version()  # e.g. 10.0.22631
        try:
            build = int(ver.split(".")[2])
        except (IndexError, ValueError):
            build = 0
        return "Windows %s (build %s)" % ("11" if build >= 22000 else platform.release(), build or ver)
    if util.IS_MAC:
        return "macOS %s (%s)" % (platform.mac_ver()[0], platform.machine())
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "%s %s" % (platform.system(), platform.release())


def cpu_model():
    if util.IS_WINDOWS:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            return platform.processor() or "unknown"
    if util.IS_MAC:
        rc, out = util.run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=10)
        return out.strip() if rc == 0 and out.strip() else platform.processor() or "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def ram_bytes():
    """Return (total, available) in bytes; None where unknown."""
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return vm.total, vm.available
        except Exception:
            pass
    if util.IS_WINDOWS:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys, st.ullAvailPhys
        except Exception:
            pass
        return None, None
    if util.IS_MAC:
        total = avail = None
        rc, out = util.run(["sysctl", "-n", "hw.memsize"], timeout=10)
        if rc == 0 and out.strip().isdigit():
            total = int(out.strip())
        rc, out = util.run(["vm_stat"], timeout=10)
        if rc == 0:
            m = re.search(r"page size of (\d+) bytes", out)
            page = int(m.group(1)) if m else 4096
            pages = {}
            for line in out.splitlines():
                mm = re.match(r"(.+?):\s+(\d+)\.", line)
                if mm:
                    pages[mm.group(1).strip()] = int(mm.group(2))
            free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
            avail = free * page if free else None
        return total, avail
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0]) * 1024
        return info.get("MemTotal"), info.get("MemAvailable", info.get("MemFree"))
    except (OSError, ValueError):
        return None, None


# ---------------------------------------------------------------- disks

def mount_point(path):
    p = os.path.abspath(str(path))
    if util.IS_WINDOWS:
        return os.path.splitdrive(p)[0] + "\\"
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


_drive_kind_cache = {}


def drive_kind(path):
    """'SSD', 'HDD' or None (unknown). Best effort, cached per drive."""
    mp = mount_point(path)
    if mp in _drive_kind_cache:
        return _drive_kind_cache[mp]
    kind = None
    try:
        if util.IS_WINDOWS:
            letter = mp[:1]
            if letter.isalpha():
                ps = ("$n=(Get-Partition -DriveLetter %s -ErrorAction Stop).DiskNumber; "
                      "(Get-PhysicalDisk | Where-Object { $_.DeviceId -eq \"$n\" }).MediaType" % letter)
                rc, out = util.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
                t = (out or "").strip().upper()
                if rc == 0 and "HDD" in t:
                    kind = "HDD"
                elif rc == 0 and "SSD" in t:
                    kind = "SSD"
        elif util.IS_MAC:
            rc, out = util.run(["diskutil", "info", "-plist", mp], timeout=20)
            if rc == 0:
                info = plistlib.loads(out.encode("utf-8"))
                if "SolidState" in info:
                    kind = "SSD" if info["SolidState"] else "HDD"
        else:
            dev = None
            best = ""
            with open("/proc/mounts", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) > 1 and mp == parts[1] and len(parts[1]) >= len(best):
                        dev, best = parts[0], parts[1]
            if dev and dev.startswith("/dev/"):
                name = os.path.basename(os.path.realpath(dev))
                sys_path = os.path.realpath("/sys/class/block/" + name)
                if os.path.exists(os.path.join(sys_path, "partition")):
                    sys_path = os.path.dirname(sys_path)
                with open(os.path.join(sys_path, "queue", "rotational")) as f:
                    kind = "HDD" if f.read().strip() == "1" else "SSD"
    except Exception:
        kind = None
    _drive_kind_cache[mp] = kind
    return kind


def disk_info(path):
    try:
        u = shutil.disk_usage(str(path))
        total, free = u.total, u.free
    except OSError:
        total = free = None
    return {"path": str(path), "mount": mount_point(path), "total": total, "free": free,
            "free_pct": (100.0 * free / total) if total else None, "kind": drive_kind(path)}


# ---------------------------------------------------------------- VRAM

def vram_nvidia():
    exe = shutil.which("nvidia-smi")
    if not exe and util.IS_WINDOWS:
        cand = r"C:\Windows\System32\nvidia-smi.exe"
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return []
    rc, out = util.run([exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], timeout=20)
    gpus = []
    if rc == 0:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    gpus.append({"name": parts[0], "vram_gb": float(parts[1]) / 1024.0,
                                 "free_gb": float(parts[2]) / 1024.0, "source": "nvidia-smi"})
                except ValueError:
                    pass
    return gpus


def vram_windows_registry():
    """AMD/Intel/NVIDIA on Windows: HardwareInformation.qwMemorySize in the display class key.
    Best effort; not verified on every driver."""
    gpus = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not sub.isdigit():
                continue
            try:
                k = winreg.OpenKey(root, sub)
                name = winreg.QueryValueEx(k, "DriverDesc")[0]
                try:
                    size = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
                except OSError:
                    size = None
                if isinstance(size, bytes):
                    size = int.from_bytes(size, "little")
                if size:
                    gpus.append({"name": name, "vram_gb": size / (1024.0 ** 3), "free_gb": None,
                                 "source": "Windows registry"})
            except OSError:
                continue
    except Exception:
        pass
    return gpus


def vram_linux_amd():
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        f = card / "device" / "mem_info_vram_total"
        if f.exists():
            try:
                total = int(f.read_text().strip())
                used_f = card / "device" / "mem_info_vram_used"
                used = int(used_f.read_text().strip()) if used_f.exists() else None
                gpus.append({"name": card.name, "vram_gb": total / (1024.0 ** 3),
                             "free_gb": (total - used) / (1024.0 ** 3) if used is not None else None,
                             "source": "Linux sysfs"})
            except (OSError, ValueError):
                pass
    return gpus


def _match_vram(device_name, table):
    """Pick the VRAM entry whose name best matches a Blender device name."""
    dn = re.sub(r"[^a-z0-9]", "", device_name.lower())
    best = None
    for g in table:
        gn = re.sub(r"[^a-z0-9]", "", g["name"].lower())
        if gn and (gn in dn or dn in gn):
            best = g
            break
    if best is None and len(table) == 1:
        best = table[0]
    return best


# ---------------------------------------------------------------- the detector

class Hardware:
    def __init__(self, cfg, locator, state_dir):
        self.cfg = cfg
        self.locator = locator
        self.state_dir = Path(state_dir)
        self.cache_path = self.state_dir / "hardware.json"
        self.lock = threading.RLock()
        self.probe = util.read_json(self.cache_path, None)
        self.detecting = False
        self.detect_error = None
        self._static = None
        self._live = None
        self._live_at = 0.0

    # -- Blender device probe (cached in state/hardware.json)
    def probe_is_current(self):
        sel = self.locator.selected()
        p = self.probe
        return bool(p and sel and p.get("blender_path") == sel["path"] and p.get("blender_version") == sel["version"]
                    and p.get("probe_version") == PROBE_VERSION)

    def redetect(self, background=True):
        with self.lock:
            if self.detecting:
                return
            self.detecting = True
            self.detect_error = None
        if background:
            threading.Thread(target=self._redetect, name="hw-detect", daemon=True).start()
        else:
            self._redetect()

    def _redetect(self):
        try:
            global _drive_kind_cache
            _drive_kind_cache = {}
            self._static = None
            self.locator.refresh()
            sel = self.locator.selected()
            if not sel:
                with self.lock:
                    self.probe = None
                    self.detect_error = "Blender not found, so GPU devices cannot be listed."
                return
            res = blender_mod.run_script(sel["path"], "probe_devices.py", {}, self.state_dir, timeout=180,
                                         log_path=Path(self.cfg.folder("logs_dir")) / "probe_devices.log")
            if not res["result"]:
                with self.lock:
                    self.detect_error = "Device probe failed. %s" % (res["error"] or "")
                return
            data = res["result"]
            data["blender_path"] = sel["path"]
            data["blender_version"] = sel["version"]
            data["detected_at"] = util.now_iso()
            with self.lock:
                self.probe = data
            util.write_json_atomic(self.cache_path, data)
            util.log("Hardware detected: %s" % ", ".join(
                "%s(%d)" % (b["type"], len(b["devices"])) for b in data.get("backends", [])) or "CPU only")
        except Exception:
            util.log_exception("hardware detection")
            with self.lock:
                self.detect_error = "Device probe crashed; see logs/renderhub.log."
        finally:
            with self.lock:
                self.detecting = False

    # -- static OS facts
    def static_info(self):
        if self._static is None:
            nv = vram_nvidia()
            reg = vram_windows_registry() if util.IS_WINDOWS else []
            lin = vram_linux_amd() if sys.platform.startswith("linux") else []
            self._static = {"os": os_name(), "cpu": cpu_model(), "threads": os.cpu_count(),
                            "apple_silicon": util.IS_MAC and platform.machine() == "arm64",
                            "vram_table": nv + reg + lin, "psutil": psutil is not None}
        return self._static

    def live_info(self):
        now = time.monotonic()
        if self._live is None or now - self._live_at > 5:
            total, avail = ram_bytes()
            self._live = {"ram_total": total, "ram_free": avail,
                          "disks": {"projects": disk_info(self.cfg.folder("projects_dir")),
                                    "output": disk_info(self.cfg.folder("output_dir"))}}
            self._live_at = now
        return self._live

    # -- the combined picture the UI and the rules use
    def summary(self, choice=None):
        """What renders a job. choice = a project's own device choice
        {"backend": "auto"|type|"CPU", "devices": [ids], "use_cpu": bool, "threads": int}; otherwise Settings."""
        st = self.static_info()
        live = self.live_info()
        probe = self.probe if self.probe_is_current() else None
        backends = (probe or {}).get("backends", [])
        usable = {b["type"]: b for b in backends if b.get("usable")}
        override, where = self.cfg.get("device_override"), "in Settings"
        if choice and choice.get("backend") and choice["backend"] != "auto":
            override, where = choice["backend"], "for this project"

        if override == "CPU":
            chosen, why = "CPU", "CPU only, chosen %s" % where
        elif override != "auto":
            chosen, why = (override, "chosen %s" % where) if override in usable else (
                None, "%s is chosen %s but this PC does not have it" % (BACKEND_LABEL.get(override, override), where))
        else:
            chosen = next((t for t in BACKEND_PRIORITY if t in usable), "CPU" if probe else None)
            why = "best available" if probe else "devices not detected yet"

        devs = usable[chosen]["devices"] if chosen and chosen != "CPU" else []
        if devs and choice and choice.get("backend") == chosen and choice.get("devices"):
            picked = [d for d in devs if d.get("id") in choice["devices"]]
            if picked:
                devs = picked
                if len(picked) < len(usable[chosen]["devices"]):
                    why += "; %d of %d GPUs picked" % (len(picked), len(usable[chosen]["devices"]))
            else:
                chosen, why, devs = None, "none of the GPUs picked for this project is on this PC", []
        use_cpu = bool(choice and chosen not in (None, "CPU") and choice.get("use_cpu")
                       and usable.get(chosen, {}).get("cpu_hybrid"))
        threads = int((choice or {}).get("threads") or 0)

        gpus = []
        if chosen and chosen != "CPU":
            for d in devs:
                g = {"name": d["name"], "id": d.get("id"), "backend": chosen, "vram_gb": None, "vram_source": None}
                if self.cfg.get("vram_gb_override"):
                    g["vram_gb"] = self.cfg.get("vram_gb_override")
                    g["vram_source"] = "entered in Settings"
                elif chosen == "METAL" and st["apple_silicon"] and live["ram_total"]:
                    g["vram_gb"] = util.gb(live["ram_total"])
                    g["vram_source"] = "unified memory (shared with the system)"
                else:
                    m = _match_vram(d["name"], st["vram_table"])
                    if m:
                        g["vram_gb"] = m["vram_gb"]
                        g["vram_source"] = m["source"]
                gpus.append(g)
        known = [g["vram_gb"] for g in gpus if g["vram_gb"]]

        return {
            "os": st["os"], "cpu": st["cpu"], "threads": st["threads"],
            "ram_total_gb": util.gb(live["ram_total"]), "ram_free_gb": util.gb(live["ram_free"]),
            "disks": live["disks"],
            "backends": backends,
            "cpu_devices": (probe or {}).get("cpu_devices", []),
            "chosen_backend": chosen,
            "chosen_label": (BACKEND_LABEL.get(chosen, chosen) or "") + (" + CPU" if use_cpu else ""),
            "chosen_reason": why, "use_cpu": use_cpu, "cpu_threads": threads,
            "devices_text": ", ".join([g["name"] for g in gpus] + ([((probe or {}).get("cpu_devices") or ["CPU"])[0] + " (CPU)"]
                                                                   if use_cpu or chosen == "CPU" else [])) +
                            (" · %d threads" % threads if threads and (use_cpu or chosen == "CPU") else ""),
            "gpus": gpus,
            "gpu_count": len(gpus),
            # one job runs on one GPU, so the smallest GPU is the limit
            "vram_gb": min(known) if gpus and len(known) == len(gpus) else None,
            "vram_unified": chosen == "METAL" and st["apple_silicon"] and not self.cfg.get("vram_gb_override"),
            "detected_at": (probe or {}).get("detected_at"),
            "detecting": self.detecting,
            "detect_error": self.detect_error,
            "probe_errors": (probe or {}).get("errors", []),
            "psutil": st["psutil"],
        }
