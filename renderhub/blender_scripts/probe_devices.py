"""Runs INSIDE Blender (background, factory startup). Lists every Cycles compute
backend and its devices, then prints one line: RENDERHUB_JSON:{...}

Usage: blender -b --factory-startup --python probe_devices.py -- <args.json>
"""

import json
import platform
import sys
import traceback

import bpy

BACKEND_ORDER = ["OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"]


def main():
    out = {
        "blender_version": bpy.app.version_string,
        "platform": sys.platform,
        "machine": platform.machine(),
        "backends": [],
        "cpu_devices": [],
        "errors": [],
        "probe_version": 2,
    }
    try:
        prefs = bpy.context.preferences
        if "cycles" not in prefs.addons:
            import addon_utils
            addon_utils.enable("cycles", default_set=True)
        cprefs = prefs.addons["cycles"].preferences
    except Exception:
        out["errors"].append("Cycles add-on not available: " + traceback.format_exc(limit=1))
        return out

    try:
        types = [t[0] for t in cprefs.get_device_types(bpy.context)]
    except Exception:
        types = list(BACKEND_ORDER)
    types = [t for t in types if t and t != "NONE"]

    cpu_names = set()
    for t in types:
        entry = {"type": t, "devices": [], "usable": False, "error": None, "cpu_hybrid": False}
        try:
            cprefs.compute_device_type = t
        except Exception as e:
            entry["error"] = "cannot select: %s" % e
            out["backends"].append(entry)
            continue
        try:
            cprefs.refresh_devices()
        except Exception:
            pass
        try:
            devices = cprefs.get_devices_for_type(t)
        except Exception:
            devices = list(cprefs.devices)
        for d in devices:
            dtype = getattr(d, "type", "")
            name = getattr(d, "name", "?")
            if dtype == "CPU":
                cpu_names.add(name)
                entry["cpu_hybrid"] = True  # Cycles lists the CPU with this backend: GPU + CPU possible
                continue
            if dtype != t:
                continue
            entry["devices"].append({"name": name, "id": getattr(d, "id", ""), "type": dtype})
        entry["usable"] = len(entry["devices"]) > 0
        out["backends"].append(entry)

    if not cpu_names:
        try:
            cprefs.compute_device_type = "NONE"
            for d in cprefs.devices:
                if getattr(d, "type", "") == "CPU":
                    cpu_names.add(d.name)
        except Exception:
            pass
    out["cpu_devices"] = sorted(cpu_names)
    return out


try:
    result = main()
except Exception:
    result = {"errors": [traceback.format_exc()], "backends": []}
print("RENDERHUB_JSON:" + json.dumps(result, ensure_ascii=True))
sys.stdout.flush()
