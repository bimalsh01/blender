"""Finished video: frames + voice/sfx + captions + on-screen text, with system ffmpeg.

The package's `video` section is the contract (keys seen in the sample packages):
  total_seconds, output, font,
  prescale {w, h, flags, unsharp}, grain {noise, unsharp},
  dips [{a, b, c, d}]  fade to black a->b, hold b->c, back c->d (seconds),
  overlays [{start, end, text, color}] + overlay_style {fontsize, y_fraction, borderw, fade_in_s},
  captions {file, fontsize, y_fraction, fontcolor, borderw, bordercolor, shadowy}
      file = JSON list of [start_s, end_s, text],
  audio {voice, sfx, sfx_volume, mix_duration first|longest|voice, pad, lead_in_s, loudnorm {I, TP}},
  end {fade_out_s, hold_black_s}, encode {preset, crf, maxrate, bufsize, audio_bitrate},
  frame_policy  "missing frame -> repeat last frame of that shot, else black" | "missing frame -> black".
The look follows the script the existing videos were made with (drawtext captions with border and
shadow, black-layer dips, loudnorm to -14 LUFS). One deliberate difference: captions and overlays show
from start up to (not including) end. between(t,start,end) includes both ends, so where one caption
ends exactly where the next starts, both were drawn on the same frame. ffmpeg runs in a work folder with the font and the
caption texts copied in, so no path or text ever needs filtergraph escaping.
"""

import glob
import json
import os
import re
import shutil
import struct
import zlib
from pathlib import Path, PurePosixPath

from . import util

# --------------------------------------------------------------- finding ffmpeg


def ffmpeg_candidates(configured):
    out = []
    if configured:
        out.append(os.path.expandvars(os.path.expanduser(configured.strip().strip('"'))))
    w = shutil.which("ffmpeg")
    if w:
        out.append(w)
    if util.IS_WINDOWS:
        la = os.environ.get("LOCALAPPDATA", "")
        out += [os.path.join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
                r"C:\ProgramData\chocolatey\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe",
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
                os.path.expanduser(r"~\scoop\shims\ffmpeg.exe")]
        out += glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"))
    else:
        out += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg", "/snap/bin/ffmpeg"]
    seen, res = set(), []
    for p in out:
        if p and p not in seen and os.path.isfile(p):
            seen.add(p)
            res.append(p)
    return res


_cache = {}


def probe_ffmpeg(path):
    """{path, version, version_tuple, drawtext, x264, ok, problem}"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, int(st.st_mtime))
    if key in _cache:
        return _cache[key]
    rc, out = util.run([path, "-hide_banner", "-version"], timeout=30)
    m = re.search(r"ffmpeg version (\S+)", out or "")
    version = m.group(1) if m else None
    mv = re.match(r"(\d+)(?:\.(\d+))?", version or "")  # "8.1.2", "7.1-full_build", "2025-11-03-git" (dated builds are new)
    vt = (int(mv.group(1)), int(mv.group(2) or 0)) if mv else None
    rc2, filters = util.run([path, "-hide_banner", "-filters"], timeout=30)
    rc3, encoders = util.run([path, "-hide_banner", "-encoders"], timeout=30)
    info = {"path": path, "version": version, "version_tuple": list(vt) if vt else None,
            "drawtext": bool(re.search(r"\sdrawtext\s", filters or "")),
            "loudnorm": bool(re.search(r"\sloudnorm\s", filters or "")),
            "x264": "libx264" in (encoders or ""), "ok": False, "problem": None}
    if rc is None or not version:
        info["problem"] = "does not run (%s)" % util.tail(out, 1)
    elif not info["drawtext"]:
        info["problem"] = "this ffmpeg has no drawtext filter (built without FreeType), so it cannot draw captions"
    elif not info["x264"]:
        info["problem"] = "this ffmpeg has no libx264 encoder"
    elif not info["loudnorm"]:
        info["problem"] = "this ffmpeg has no loudnorm filter"
    else:
        info["ok"] = True
    _cache[key] = info
    return info


def find_ffmpeg(configured=""):
    """The first usable ffmpeg, or a dict with ok=False and a plain explanation."""
    tried = []
    for p in ffmpeg_candidates(configured):
        info = probe_ffmpeg(p)
        if info and info["ok"]:
            return info
        if info:
            tried.append("%s: %s" % (p, info["problem"]))
    if configured and not os.path.isfile(os.path.expanduser(configured)):
        tried.insert(0, "the ffmpeg path in Settings does not exist: %s" % configured)
    hint = ("Install it (for example: winget install Gyan.FFmpeg) or put the path to ffmpeg.exe in Settings."
            if util.IS_WINDOWS else "Install it (for example: brew install ffmpeg) or put its path in Settings.")
    return {"ok": False, "path": None, "version": None,
            "problem": ("ffmpeg with drawtext was not found. " + hint + (" Tried: " + "; ".join(tried) if tried else ""))}


# --------------------------------------------------------------- package inputs


def _safe(rel):
    if not isinstance(rel, str) or not rel.strip():
        return False
    r = rel.replace("\\", "/")
    return not r.startswith("/") and not re.match(r"^[A-Za-z]:", r) and ".." not in PurePosixPath(r).parts


def _pkg(folder, rel):
    return Path(folder).joinpath(*PurePosixPath(rel.replace("\\", "/")).parts)


def load_captions(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("captions file must be a JSON list of [start, end, text]")
    out = []
    for i, c in enumerate(data):
        if not (isinstance(c, (list, tuple)) and len(c) >= 3):
            raise ValueError("caption %d is not [start, end, text]" % (i + 1))
        s, e, t = float(c[0]), float(c[1]), str(c[2])
        if e > s and t.strip():
            out.append((s, e, t))
    return out


def check_inputs(folder, video):
    """(problems, notes) for the finished-video inputs of a package."""
    problems, notes = [], []
    if not isinstance(video, dict):
        return ["the video section is not an object"], notes
    font = video.get("font")
    if font:
        if not _safe(font) or not _pkg(folder, font).is_file():
            problems.append("font %s is missing" % font)
    else:
        problems.append("no font given (video.font)")
    cap = video.get("captions") or {}
    if cap.get("file"):
        p = _pkg(folder, cap["file"]) if _safe(cap["file"]) else None
        if not p or not p.is_file():
            problems.append("captions file %s is missing" % cap["file"])
        else:
            try:
                notes.append("%d captions" % len(load_captions(p)))
            except (ValueError, TypeError) as e:
                problems.append("captions file %s: %s" % (cap["file"], e))
    audio = video.get("audio") or {}
    for key in ("voice", "sfx"):
        rel = audio.get(key)
        if rel and (not _safe(rel) or not _pkg(folder, rel).is_file()):
            problems.append("%s audio %s is missing" % (key, rel))
    if not audio.get("voice"):
        notes.append("no voice track: the video will be silent unless sfx is given")
    if video.get("output") and not _safe(video["output"]):
        problems.append("video.output must be a relative path, got %r" % video["output"])
    for k in ("sfx_duck",):
        if audio.get(k):
            notes.append("audio.%s is not supported yet and is ignored" % k)
    return problems, notes


# --------------------------------------------------------------- building the command


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)
    return struct.unpack(">II", head[16:24])


def write_black_png(path, w, h):
    """A black RGB PNG, stdlib only (for frame_policy 'black')."""
    raw = b"".join(b"\x00" + b"\x00" * (w * 3) for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _num(x):
    return ("%.4f" % float(x)).rstrip("0").rstrip(".")


def frame_list(frames, policy, work_dir):
    """frames: [(shot, frame, path)] in play order. Applies frame_policy to missing frames.
    Returns (paths, n_repeated, n_black)."""
    policy = (policy or "").lower()
    repeat = "repeat" in policy
    present = [p for _, _, p in frames if os.path.isfile(p) and os.path.getsize(p) > 0]
    if not present:
        raise ValueError("no rendered frames found")
    paths, last_by_shot, rep, black = [], {}, 0, 0
    black_png = None
    for shot, _, p in frames:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            paths.append(p)
            last_by_shot[shot] = p
        elif repeat and shot in last_by_shot:
            paths.append(last_by_shot[shot])
            rep += 1
        else:
            if black_png is None:
                w, h = png_size(present[0])
                black_png = os.path.join(work_dir, "black.png")
                write_black_png(black_png, w, h)
            paths.append(black_png)
            black += 1
    return paths, rep, black


def build(manifest, folder, frames, work_dir, out_path, ffmpeg):
    """Prepare work_dir and return (cmd, info). cmd must run with cwd=work_dir.
    info: total_seconds, out_frames, notes (list)."""
    v = manifest.get("video") or {}
    r = manifest["render"]
    fps = int(r.get("fps") or 30)
    work = Path(work_dir)
    (work / "t").mkdir(parents=True, exist_ok=True)
    notes = []

    paths, rep, black = frame_list(frames, v.get("frame_policy"), str(work))
    if rep or black:
        notes.append("%d missing frames filled (%d repeated, %d black) per frame_policy" % (rep + black, rep, black))
    with open(work / "frames.txt", "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p in paths + [paths[-1]]:  # the concat demuxer needs the last file twice
            f.write("file '%s'\nduration %.6f\n" % (Path(p).as_posix().replace("'", "'\\''"), 1.0 / fps))

    total = float(v.get("total_seconds") or len(paths) / float(fps))
    end = v.get("end") or {}
    fade_out = float(end.get("fade_out_s") or 0)
    hold = float(end.get("hold_black_s") or 0)
    total_out = total + hold

    fw, fh = png_size(paths[0])
    pre = v.get("prescale") or None
    W, H = (int(pre["w"]), int(pre["h"])) if pre else (fw - fw % 2, fh - fh % 2)

    shutil.copyfile(_pkg(folder, v["font"]), work / "font.ttf")

    chain = ["fps=%d" % fps]
    if pre:
        chain.append("scale=%d:%d:flags=%s" % (W, H, pre.get("flags") or "lanczos"))
        if pre.get("unsharp"):
            chain.append("unsharp=%s" % pre["unsharp"])
    else:
        chain.append("scale=%d:%d" % (W, H))
    grain = v.get("grain") or None
    if grain:
        if grain.get("noise"):
            chain.append("noise=alls=%s:allf=t" % _num(grain["noise"]))
        if grain.get("unsharp"):
            chain.append("unsharp=%s" % grain["unsharp"])
    chain.append("format=yuv420p")
    fc = ["[0:v]" + ",".join(chain) + "[v0]"]
    last = "v0"

    for j, d in enumerate(v.get("dips") or []):
        a, b, c, e = (float(d[k]) for k in "abcd")
        fc.append("color=black:s=%dx%d:r=%d:d=%s,format=rgba,fade=t=in:st=0:d=%s:alpha=1,fade=t=out:st=%s:d=%s:alpha=1,"
                  "setpts=PTS+%s/TB[k%d]" % (W, H, fps, _num(e - a + 0.05), _num(max(b - a, 0.001)), _num(c - a),
                                            _num(max(e - c, 0.001)), _num(a), j))
        fc.append("[%s][k%d]overlay=eof_action=pass:enable='between(t,%s,%s)'[d%d]" % (last, j, _num(a), _num(e), j))
        last = "d%d" % j

    ost = v.get("overlay_style") or {}
    for j, o in enumerate(v.get("overlays") or []):
        if o.get("dynamic"):
            notes.append("overlay %r is dynamic and not supported yet; skipped" % o.get("text"))
            continue
        s, e = float(o["start"]), float(o["end"])
        (work / "t" / ("o%d.txt" % j)).write_text(str(o["text"]), encoding="utf-8")
        fi = float(ost.get("fade_in_s") or 0)
        alpha = (":alpha='if(lt(t,%s),max(0,(t-%s)/%s),1)'" % (_num(s + fi), _num(s), _num(fi))) if fi > 0 else ""
        fc.append("[%s]drawtext=fontfile=font.ttf:textfile=t/o%d.txt:expansion=none:fontsize=%s:fontcolor=%s:"
                  "borderw=%s:bordercolor=black:x=(w-tw)/2:y=h*%s-th/2:enable='gte(t,%s)*lt(t,%s)'%s[o%d]"
                  % (last, j, _num(ost.get("fontsize", 120)), o.get("color") or "white", _num(ost.get("borderw", 8)),
                     _num(ost.get("y_fraction", 0.12)), _num(s), _num(e), alpha, j))
        last = "o%d" % j

    cs = v.get("captions") or {}
    caps = load_captions(_pkg(folder, cs["file"])) if cs.get("file") else []
    for j, (s, e, t) in enumerate(caps):
        (work / "t" / ("c%d.txt" % j)).write_text(t, encoding="utf-8")
        fc.append("[%s]drawtext=fontfile=font.ttf:textfile=t/c%d.txt:expansion=none:fontsize=%s:fontcolor=%s:"
                  "borderw=%s:bordercolor=%s:shadowx=0:shadowy=%s:shadowcolor=black@0.5:x=(w-tw)/2:y=h*%s-th/2:"
                  "enable='gte(t,%s)*lt(t,%s)'[c%d]"
                  % (last, j, _num(cs.get("fontsize", 84)), cs.get("fontcolor") or "white", _num(cs.get("borderw", 7)),
                     cs.get("bordercolor") or "black@0.95", _num(cs.get("shadowy", 6)), _num(cs.get("y_fraction", 0.75)),
                     _num(s), _num(e), j))
        last = "c%d" % j

    tail = []
    if fade_out > 0:
        tail.append("fade=t=out:st=%s:d=%s" % (_num(max(0, total - fade_out)), _num(fade_out)))
    if hold > 0:
        tail.append("tpad=stop_mode=add:stop_duration=%s:color=black" % _num(hold))
    if tail:
        fc.append("[%s]%s[vend]" % (last, ",".join(tail)))
        last = "vend"
    vout = last

    # audio
    au = v.get("audio") or {}
    inputs = ["-f", "concat", "-safe", "0", "-i", "frames.txt"]
    n_in = 1
    voice = str(_pkg(folder, au["voice"])) if au.get("voice") else None
    sfx = str(_pkg(folder, au["sfx"])) if au.get("sfx") else None
    alabels = []
    if voice:
        inputs += ["-i", voice]
        lead = float(au.get("lead_in_s") or 0)
        fc.append("[%d:a]aresample=48000%s[vo]" % (n_in, (",adelay=%d:all=1" % int(lead * 1000)) if lead else ""))
        alabels.append("vo")
        n_in += 1
    if sfx:
        inputs += ["-i", sfx]
        fc.append("[%d:a]aresample=48000,volume=%s[sx]" % (n_in, _num(au.get("sfx_volume", 1.0))))
        alabels.append("sx")
        n_in += 1
    aout = None
    if alabels:
        if len(alabels) == 2:
            mode = {"longest": "longest", "first": "first", "voice": "first", "shortest": "shortest"}.get(
                str(au.get("mix_duration") or "longest"), "longest")
            fc.append("[vo][sx]amix=inputs=2:duration=%s:normalize=0[mx]" % mode)
            cur = "mx"
        else:
            cur = alabels[0]
        post = ["apad"]  # silence after the voice, up to the end of the picture
        ln = au.get("loudnorm")
        if ln:
            post.append("loudnorm=I=%s:TP=%s" % (_num(ln.get("I", -14)), _num(ln.get("TP", -1.5))))
            post.append("aresample=48000")
        post.append("atrim=0:%s" % _num(total_out))
        if fade_out > 0:
            post.append("afade=t=out:st=%s:d=%s" % (_num(max(0, total - fade_out)), _num(fade_out)))
        fc.append("[%s]%s[aout]" % (cur, ",".join(post)))
        aout = "aout"
    else:
        notes.append("no audio in the video section: the video is silent")

    with open(work / "filter.txt", "w", encoding="utf-8") as f:
        f.write(";\n".join(fc))

    vt = tuple(ffmpeg.get("version_tuple") or ())
    script_opt = ["-filter_complex_script", "filter.txt"] if vt and vt[0] < 7 else ["-/filter_complex", "filter.txt"]
    enc = v.get("encode") or {}
    cmd = [ffmpeg["path"], "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"] + inputs
    cmd += script_opt + ["-map", "[%s]" % vout]
    if aout:
        cmd += ["-map", "[%s]" % aout, "-c:a", "aac", "-b:a", str(enc.get("audio_bitrate") or "192k")]
    cmd += ["-c:v", "libx264", "-preset", str(enc.get("preset") or "medium"), "-crf", _num(enc.get("crf", 18))]
    if enc.get("maxrate"):
        cmd += ["-maxrate", str(enc["maxrate"]), "-bufsize", str(enc.get("bufsize") or enc["maxrate"])]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(fps), "-t", _num(total_out), "-movflags", "+faststart", str(out_path)]
    return cmd, {"total_seconds": total_out, "out_frames": int(round(total_out * fps)), "notes": notes,
                 "captions": len(caps), "size": [W, H]}


def output_rel(manifest, project):
    """Where the finished video goes, relative to output/<project>/."""
    out = (manifest.get("video") or {}).get("output")
    if out and _safe(out) and out.lower().endswith(".mp4"):
        return out.replace("\\", "/")
    return "final/%s.mp4" % project
