"""Runs INSIDE Blender (background, factory startup). Renders frames for one or more
shots of a project package.

For each task (one shot): reload the .blend so fixes never leak between shots, choose the
compute device, apply the manifest render settings, the shot's samples override, then the
global fixes and the shot's fixes (fixes run last, so they win). Each frame is rendered to a
hidden temp file and renamed into place, so a killed render never leaves a half-written frame.
Frames that already exist are skipped, which is how interrupted renders resume.

Every event is one stdout line:  RENDERHUB_EVENT:{"event": ..., ...}
Exit codes: 0 done, 2 error, 4 disk full, 5 RenderHub went away.

Args JSON:
{"package": dir, "blend": path, "render": {...manifest render...}, "backend": "METAL"|"CPU"|...,
 "device_ids": [..] or [], "global_fixes": [abs paths], "skip_existing": true, "parent_pid": int,
 "tasks": [{"shot": "S01", "scene_name": null, "camera": null, "samples": null,
            "fixes": [abs paths], "frames": [1,2,3], "out_dir": dir, "name": "frame_{frame:05d}.png"}]}
"""

import json
import os
import shutil
import sys
import time
import traceback

import bpy

EV = "RENDERHUB_EVENT:"


class RenderError(Exception):
    def __init__(self, message, code=2, kind="error"):
        super().__init__(message)
        self.code, self.kind = code, kind


def emit(event, **kw):
    kw["event"] = event
    print(EV + json.dumps(kw, ensure_ascii=True), flush=True)


def png_complete(path):
    try:
        size = os.path.getsize(path)
        if size < 64:
            return False
        with open(path, "rb") as f:
            f.seek(size - 12)
            return b"IEND" in f.read()
    except OSError:
        return False


def parent_alive(ppid):
    if not ppid or os.name != "posix":
        return True
    return os.getppid() == ppid


def set_device(backend, device_ids, use_cpu=False):
    if backend in (None, "", "CPU"):
        return "CPU", ["CPU"]
    cprefs = bpy.context.preferences.addons["cycles"].preferences
    try:
        cprefs.compute_device_type = backend
    except TypeError:
        raise RenderError("Blender on this PC does not offer the %s backend. Press Re-detect in RenderHub." % backend)
    try:
        cprefs.refresh_devices()
    except Exception:
        pass
    try:
        devices = list(cprefs.get_devices_for_type(backend))
    except Exception:
        devices = list(cprefs.devices)
    used = []
    for d in cprefs.devices:
        d.use = False
    for d in devices:
        use = d.type == backend and (not device_ids or d.id in device_ids)
        d.use = use
        if use:
            used.append(d.name)
    gpu_count = len(used)
    if use_cpu:
        for d in cprefs.devices:
            if d.type == "CPU":
                d.use = True
                used.append(d.name + " (CPU)")
    if not gpu_count:
        raise RenderError("No %s device is available%s. Press Re-detect in RenderHub."
                          % (backend, " (wanted %s)" % ", ".join(device_ids) if device_ids else ""))
    return "GPU", used


def apply_settings(scn, render, samples, device_mode, threads=0):
    try:
        scn.render.engine = "CYCLES"
    except TypeError:
        raise RenderError("Cycles is not available in this Blender.")
    scn.cycles.device = device_mode
    if threads:
        scn.render.threads_mode = "FIXED"
        scn.render.threads = int(threads)
    else:
        scn.render.threads_mode = "AUTO"
    scn.cycles.samples = int(samples)
    scn.cycles.use_denoising = bool(render.get("denoise", True))
    scn.render.use_persistent_data = bool(render.get("persistent_data", False))
    scn.render.resolution_x = int(render["resolution_x"])
    scn.render.resolution_y = int(render["resolution_y"])
    scn.render.resolution_percentage = 100
    scn.render.fps = int(render["fps"])
    scn.render.fps_base = 1.0
    im = scn.render.image_settings
    try:
        im.media_type = "IMAGE"  # Blender 5+
    except (AttributeError, TypeError):
        pass
    im.file_format = "PNG"
    depth = str(render.get("color_depth") or "8")
    try:
        im.color_depth = depth if depth in ("8", "16") else "8"
    except TypeError:
        pass
    im.color_mode = "RGBA" if scn.render.film_transparent else "RGB"
    im.compression = 15


def as_context_scene(scn):
    """Make scn the context scene. Rendering or editing a scene that is not the file's active
    scene without this gives wrong frames (verified 2026-11-23 on Never Slept S16B: 22% off;
    with it, pixel-identical to Blender's own animation render)."""
    return bpy.context.temp_override(scene=scn)


def run_fix(path, scn, shot):
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        code = compile(src, path, "exec")
        exec(code, {"__name__": "__renderhub_fix__", "__file__": path, "bpy": bpy, "scene": scn,
                    "shot": shot})
    except Exception:
        raise RenderError("Fix script %s failed:\n%s" % (os.path.basename(path), traceback.format_exc(limit=3)))
    emit("fix_applied", file=os.path.basename(path), shot=shot)


def python_driver_count():
    n = 0
    colls = (bpy.data.objects, bpy.data.armatures, bpy.data.meshes, bpy.data.materials,
             bpy.data.shape_keys, bpy.data.scenes, bpy.data.node_groups)
    for coll in colls:
        for idb in coll:
            ad = getattr(idb, "animation_data", None)
            if not ad:
                continue
            for fc in ad.drivers:
                d = fc.driver
                if d.type == "SCRIPTED" and not getattr(d, "is_simple_expression", True):
                    n += 1
    return n


def check_disk(out_dir, last_size):
    free = shutil.disk_usage(out_dir).free
    need = max(3 * (last_size or 0), 50 * 1024 * 1024) + 200 * 1024 * 1024
    if free < need:
        raise RenderError("Disk almost full: %.0f MB free on the output drive. Rendering stopped; "
                          "frames so far are kept. Free up space, then Resume." % (free / 1048576.0),
                          code=4, kind="disk_full")


def run(args):
    blend = args["blend"]
    render = args["render"]
    ppid = args.get("parent_pid")
    mode, used = set_device(args.get("backend"), args.get("device_ids") or [], args.get("use_cpu"))
    emit("start", blender=bpy.app.version_string, backend=args.get("backend") or "CPU", devices=used,
         threads=args.get("threads") or "all", tasks=len(args["tasks"]))
    n_py = python_driver_count()
    if n_py:
        emit("warning", message="%d drivers use Python expressions; auto-run scripts are off, so they may "
                                "not update. Check the test frames." % n_py)
    last_size = 0
    for ti, task in enumerate(args["tasks"]):
        shot = task["shot"]
        if ti > 0:
            bpy.ops.wm.open_mainfile(filepath=blend, load_ui=False)
            mode, used = set_device(args.get("backend"), args.get("device_ids") or [], args.get("use_cpu"))
        scn = bpy.context.scene
        if task.get("scene_name"):
            scn = bpy.data.scenes.get(task["scene_name"])
            if scn is None:
                raise RenderError("Shot %s: scene '%s' is not in the .blend." % (shot, task["scene_name"]))
        if task.get("camera"):
            cam = bpy.data.objects.get(task["camera"])
            if cam is None or cam.type != "CAMERA":
                raise RenderError("Shot %s: camera '%s' is not in the .blend." % (shot, task["camera"]))
            scn.camera = cam
        if scn.camera is None:
            raise RenderError("Shot %s: scene '%s' has no camera." % (shot, scn.name))
        samples = task.get("samples") or render.get("samples") or 128
        apply_settings(scn, render, samples, mode, args.get("threads") or 0)
        with as_context_scene(scn):  # fixes may use bpy.context.scene
            for fx in args.get("global_fixes", []) + task.get("fixes", []):
                run_fix(fx, scn, shot)
        emit("shot_start", shot=shot, scene=scn.name, camera=scn.camera.name, samples=scn.cycles.samples,
             denoise=scn.cycles.use_denoising, frames=len(task["frames"]))

        out_dir = task["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        for f in os.listdir(out_dir):  # our own leftovers from a killed render
            if f.startswith(".partial_"):
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
        for frame in task["frames"]:
            if not parent_alive(ppid):
                raise RenderError("RenderHub is no longer running; stopping.", code=5, kind="orphaned")
            out = os.path.join(out_dir, task["name"].format(frame=frame, shot=shot))
            if args.get("skip_existing", True) and png_complete(out):
                emit("frame_skip", shot=shot, frame=frame)
                continue
            check_disk(out_dir, last_size)
            tmp = os.path.join(out_dir, ".partial_%s_%d.png" % (shot, frame))
            emit("frame_start", shot=shot, frame=frame)
            t0 = time.time()
            with as_context_scene(scn):
                scn.frame_set(frame)
                bpy.ops.render.render(write_still=False, scene=scn.name)
            try:
                bpy.data.images["Render Result"].save_render(filepath=tmp, scene=scn)
            except OSError as e:
                if "space" in str(e).lower():
                    raise RenderError("Disk full while saving frame %d." % frame, code=4, kind="disk_full")
                raise
            if not png_complete(tmp):
                raise RenderError("Frame %d was not written (%s)." % (frame, tmp))
            os.replace(tmp, out)
            last_size = os.path.getsize(out)
            emit("frame_done", shot=shot, frame=frame, seconds=round(time.time() - t0, 3), path=out,
                 bytes=last_size)
    emit("done")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], "r", encoding="utf-8") as f:
        args = json.load(f)
    try:
        run(args)
        return 0
    except RenderError as e:
        emit("error", kind=e.kind, message=str(e))
        print("RENDERHUB_ERROR: %s" % e, flush=True)
        return e.code
    except Exception:
        tb = traceback.format_exc()
        emit("error", kind="exception", message=tb)
        print("RENDERHUB_ERROR: %s" % tb, flush=True)
        return 2


code = main()
sys.stdout.flush()
os._exit(code)  # exit now with our code; no further Blender processing
