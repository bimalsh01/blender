"""Runs INSIDE Blender with the project's .blend open (background, factory startup,
auto-run scripts disabled). Reports missing external files, missing linked libraries,
missing fonts, fix scripts that do not compile, and shot scenes/cameras that do not exist.
Prints one line: RENDERHUB_JSON:{...}

Args JSON: {"package": "<folder>", "fixes": ["fixes/a.py", ...],
            "shots": [{"id": "S01", "scene_name": null, "camera": null}, ...]}
"""

import json
import os
import sys
import traceback

import bpy

LIMIT = 200  # cap list lengths in the report


def load_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    with open(argv[0], "r", encoding="utf-8") as f:
        return json.load(f)


def native(path):
    if os.sep == "/":
        path = path.replace("\\", "/")
    return os.path.normpath(path)


def abs_of(idblock, filepath):
    try:
        p = bpy.path.abspath(filepath, library=getattr(idblock, "library", None))
    except Exception:
        p = bpy.path.abspath(filepath)
    return native(p)


def is_relative(filepath):
    return filepath.startswith("//")


def real_users(idblock):
    return idblock.users - (1 if getattr(idblock, "use_fake_user", False) else 0)


def check_images():
    rep = {"total": 0, "packed": 0, "generated": 0, "external": 0, "missing": [], "missing_unused": [],
           "absolute": []}
    for img in bpy.data.images:
        rep["total"] += 1
        if img.source in {"GENERATED", "VIEWER"}:
            rep["generated"] += 1
            continue
        if img.packed_file is not None or getattr(img, "packed_files", None) and len(img.packed_files):
            rep["packed"] += 1
            continue
        rep["external"] += 1
        fp = img.filepath or img.filepath_raw or ""
        if not fp:
            entry = {"name": img.name, "path": "(no file path)"}
            (rep["missing"] if real_users(img) > 0 else rep["missing_unused"]).append(entry)
            continue
        if not is_relative(fp):
            rep["absolute"].append({"name": img.name, "path": fp})
        p = abs_of(img, fp)
        ok = os.path.exists(p)
        if not ok and img.source == "TILED":
            ok = all(os.path.exists(p.replace("<UDIM>", str(t.number))) for t in img.tiles) if "<UDIM>" in p else False
        if not ok and img.source == "SEQUENCE":
            ok = os.path.isdir(os.path.dirname(p))
        if not ok:
            entry = {"name": img.name, "path": fp}
            (rep["missing"] if real_users(img) > 0 else rep["missing_unused"]).append(entry)
    for k in ("missing", "missing_unused", "absolute"):
        rep[k + "_count"] = len(rep[k])
        rep[k] = rep[k][:LIMIT]
    return rep


def check_collection(coll, path_attr="filepath", skip=None):
    rep = {"total": 0, "packed": 0, "missing": [], "absolute": []}
    for item in coll:
        rep["total"] += 1
        fp = getattr(item, path_attr, "") or ""
        if skip and skip(item, fp):
            continue
        if getattr(item, "packed_file", None) is not None:
            rep["packed"] += 1
            continue
        if not fp:
            continue
        if not is_relative(fp):
            rep["absolute"].append({"name": item.name, "path": fp})
        if not os.path.exists(abs_of(item, fp)):
            rep["missing"].append({"name": item.name, "path": fp})
    rep["missing_count"] = len(rep["missing"])
    rep["absolute_count"] = len(rep["absolute"])
    rep["missing"] = rep["missing"][:LIMIT]
    rep["absolute"] = rep["absolute"][:LIMIT]
    return rep


def check_libraries():
    rep = {"total": 0, "missing": [], "absolute": []}
    for lib in bpy.data.libraries:
        rep["total"] += 1
        fp = lib.filepath or ""
        if fp and not is_relative(fp):
            rep["absolute"].append({"name": lib.name, "path": fp})
        missing = getattr(lib, "is_missing", None)
        if missing is None:
            missing = not os.path.exists(abs_of(lib, fp))
        if missing:
            rep["missing"].append({"name": lib.name, "path": fp})
    rep["missing_count"] = len(rep["missing"])
    rep["absolute_count"] = len(rep["absolute"])
    return rep


def check_fixes(package, fixes):
    out = []
    for rel in fixes:
        p = os.path.join(package, *rel.replace("\\", "/").split("/"))
        entry = {"file": rel, "ok": True, "error": None}
        try:
            with open(p, "r", encoding="utf-8") as f:
                src = f.read()
            compile(src, p, "exec")
        except FileNotFoundError:
            entry.update(ok=False, error="file not found")
        except SyntaxError as e:
            entry.update(ok=False, error="syntax error line %s: %s" % (e.lineno, e.msg))
        except Exception as e:
            entry.update(ok=False, error=str(e))
        out.append(entry)
    return out


def check_shots(shots):
    scenes = {s.name: s for s in bpy.data.scenes}
    out = []
    for s in shots:
        probs = []
        scn = bpy.context.scene
        if s.get("scene_name"):
            scn = scenes.get(s["scene_name"])
            if scn is None:
                probs.append("scene '%s' does not exist in the .blend" % s["scene_name"])
        if s.get("camera"):
            ob = bpy.data.objects.get(s["camera"])
            if ob is None:
                probs.append("camera '%s' does not exist" % s["camera"])
            elif ob.type != "CAMERA":
                probs.append("'%s' is a %s, not a camera" % (s["camera"], ob.type.lower()))
        elif scn is not None and scn.camera is None:
            probs.append("scene '%s' has no active camera" % scn.name)
        out.append({"id": s["id"], "problems": probs})
    return out


def main():
    args = load_args()
    scene = bpy.context.scene
    other_missing = []
    try:
        checked = set()
        for p in bpy.utils.blend_paths(absolute=True, packed=False, local=False):
            ap = native(p)
            if ap in checked:
                continue
            checked.add(ap)
            if "<UDIM>" in ap or "#" in os.path.basename(ap):
                continue
            if not os.path.exists(ap):
                other_missing.append(p)
    except Exception:
        pass
    images = check_images()
    known = set()
    for coll in (bpy.data.images, bpy.data.libraries, bpy.data.fonts):
        for item in coll:
            fp = getattr(item, "filepath", "")
            if fp:
                known.add(abs_of(item, fp))
    other_missing = [p for p in other_missing if native(p) not in known]

    return {
        "ok": True,
        "blender_version": bpy.app.version_string,
        "file_version": "%d.%d" % tuple(bpy.data.version[:2]),
        "engine": scene.render.engine,
        "scene": scene.name,
        "scenes": [s.name for s in bpy.data.scenes],
        "frame_range": [scene.frame_start, scene.frame_end],
        "fps": scene.render.fps,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage],
        "images": images,
        "libraries": check_libraries(),
        "fonts": check_collection(bpy.data.fonts, skip=lambda f, fp: fp == "<builtin>"),
        "sounds": check_collection(bpy.data.sounds),
        "movieclips": check_collection(bpy.data.movieclips),
        "volumes": check_collection(bpy.data.volumes),
        "cache_files": check_collection(bpy.data.cache_files),
        "other_missing": other_missing[:LIMIT],
        "other_missing_count": len(other_missing),
        "fixes": check_fixes(args["package"], args.get("fixes", [])),
        "shots": check_shots(args.get("shots", [])),
        "objects": len(bpy.data.objects),
        "actions": len(bpy.data.actions),
    }


try:
    result = main()
except Exception:
    result = {"ok": False, "error": traceback.format_exc()}
print("RENDERHUB_JSON:" + json.dumps(result, ensure_ascii=True))
sys.stdout.flush()
