"""Runs INSIDE Blender (background, factory startup). Joins rendered PNG frames into an
H.264 MP4 with Blender's built-in FFmpeg, so no separate ffmpeg install is needed.

Args JSON: {"frames": [abs png paths in order], "fps": 30, "width": 1080, "height": 1920,
            "out": "<final .mp4>", "parent_pid": int}
The video is written to a hidden temp file and renamed when complete.
Events: RENDERHUB_EVENT:{"event": "start"|"frame_done"|"done"|"error", ...}
"""

import json
import os
import sys
import time
import traceback

import bpy

EV = "RENDERHUB_EVENT:"


def emit(event, **kw):
    kw["event"] = event
    print(EV + json.dumps(kw, ensure_ascii=True), flush=True)


def set_enum(obj, prop, *choices):
    """Set the first value this Blender accepts (enum names move between versions)."""
    errors = []
    for c in choices:
        try:
            setattr(obj, prop, c)
            return c
        except (TypeError, ValueError) as e:
            errors.append(str(e))
    raise RuntimeError("None of %s is valid for %s: %s" % (choices, prop, errors[-1] if errors else ""))


def main(args):
    frames = args["frames"]
    if not frames:
        raise RuntimeError("No frames to join.")
    out = os.path.abspath(args["out"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = os.path.join(os.path.dirname(out), ".partial_" + os.path.basename(out))
    if os.path.exists(tmp):
        os.remove(tmp)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scn = bpy.context.scene
    scn.render.resolution_x = int(args["width"])
    scn.render.resolution_y = int(args["height"])
    scn.render.resolution_percentage = 100
    scn.render.fps = int(args["fps"])
    scn.render.fps_base = 1.0
    # Frames are already final sRGB images: no view transform on top.
    set_enum(scn.view_settings, "view_transform", "Standard")
    try:
        scn.view_settings.look = "None"
    except TypeError:
        pass
    scn.view_settings.exposure = 0.0
    scn.view_settings.gamma = 1.0

    se = scn.sequence_editor_create()
    strips = se.strips if hasattr(se, "strips") else se.sequences  # renamed in Blender 4.4/5
    # One image strip per folder (shot), placed back to back.
    groups = []
    for p in frames:
        d = os.path.dirname(p)
        if groups and groups[-1][0] == d:
            groups[-1][1].append(os.path.basename(p))
        else:
            groups.append((d, [os.path.basename(p)]))
    start = 1
    for i, (d, names) in enumerate(groups):
        s = strips.new_image(name="part%d" % i, filepath=os.path.join(d, names[0]), channel=1, frame_start=start)
        for n in names[1:]:
            s.elements.append(n)
        start += len(names)
    scn.frame_start = 1
    scn.frame_end = start - 1

    im = scn.render.image_settings
    try:
        set_enum(im, "media_type", "VIDEO")  # Blender 5+
    except (KeyError, RuntimeError, AttributeError):
        pass
    set_enum(im, "file_format", "FFMPEG")
    ff = scn.render.ffmpeg
    set_enum(ff, "format", "MPEG4")
    set_enum(ff, "codec", "H264")
    try:
        set_enum(ff, "constant_rate_factor", "HIGH")
        set_enum(ff, "ffmpeg_preset", "GOOD")
    except RuntimeError:
        pass
    try:
        set_enum(ff, "audio_codec", "NONE")
    except RuntimeError:
        pass
    scn.render.use_file_extension = False
    scn.render.filepath = tmp

    total = scn.frame_end
    t0 = time.time()
    ppid = args.get("parent_pid")

    def on_frame(scene, *_):
        if ppid and os.name == "posix" and os.getppid() != ppid:
            os._exit(5)  # RenderHub went away
        emit("frame_done", shot="video", frame=scene.frame_current, seconds=0)

    bpy.app.handlers.render_post.append(on_frame)
    emit("start", frames=total, out=out)
    bpy.ops.render.render(animation=True, scene=scn.name)
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise RuntimeError("Blender did not write the video file.")
    os.replace(tmp, out)
    emit("done", path=out, bytes=os.path.getsize(out), seconds=round(time.time() - t0, 1), frames=total)


try:
    with open(sys.argv[sys.argv.index("--") + 1], "r", encoding="utf-8") as f:
        main(json.load(f))
    code = 0
except Exception:
    emit("error", kind="exception", message=traceback.format_exc())
    code = 2
sys.stdout.flush()
os._exit(code)
