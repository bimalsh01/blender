"""The per-project checklist: each rule returns pass / warn / fail (or skip / pending)
with a plain sentence and a suggested fix. Overall status:
Invalid > Blocked > Checking... > Warnings > Ready."""

from . import util
from .package import rule


def _ver_tuple(s):
    parts = []
    for p in str(s or "").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def estimated_disk_gb(manifest, total_frames):
    """PNG frames: about 0.6 of raw RGBA size; 16-bit doubles it."""
    r = manifest["render"]
    depth = 2 if str(r.get("color_depth")) == "16" else 1
    per_frame = r["resolution_x"] * r["resolution_y"] * 4 * depth * 0.6
    return total_frames * per_frame / (1024.0 ** 3)


def rule_blender(manifest, blender):
    title = "Blender version"
    sel = blender.get("selected")
    if not sel:
        return rule("blender", title, "fail", blender.get("error") or "Blender not found.",
                    "Install Blender or set its path in Settings.")
    have = _ver_tuple(sel["version"])
    need = _ver_tuple(manifest["blender"]["min_version"])
    built = _ver_tuple(manifest["blender"]["built_with"])
    if need and have < need:
        return rule("blender", title, "fail",
                    "Blender %s is installed; this project needs %s or newer." % (sel["version"], manifest["blender"]["min_version"]),
                    "Install a newer Blender and select it in Settings.")
    if built and have[:2] != built[:2]:
        return rule("blender", title, "warn",
                    "Blender %s here; the project was built with %s. Renders may differ: check test frames."
                    % (sel["version"], manifest["blender"]["built_with"]))
    return rule("blender", title, "pass", "Blender %s (%s)." % (sel["version"], sel["path"]))


def rule_gpu(manifest, hw):
    title = "GPU backend"
    chosen = hw["chosen_backend"]
    if chosen is None:
        if hw.get("detecting"):
            return rule("gpu", title, "pending", "Detecting devices...")
        if hw.get("detect_error"):
            return rule("gpu", title, "fail", hw["detect_error"], "Fix Blender, then press Re-detect.")
        return rule("gpu", title, "fail", "No usable render device: %s." % hw.get("chosen_reason"),
                    "Set Device to auto in Settings, or press Re-detect.")
    if chosen == "CPU":
        reason = hw.get("chosen_reason")
        if manifest["requirements"]["gpu_required"]:
            return rule("gpu", title, "fail", "This project needs a GPU, but rendering would use the CPU (%s)." % reason,
                        "Use a PC with an NVIDIA, AMD, Apple or Intel GPU, or set Device to auto.")
        return rule("gpu", title, "warn", "CPU only (%s): rendering will be slow." % reason)
    names = ", ".join(g["name"] for g in hw["gpus"])
    return rule("gpu", title, "pass", "%s: %s." % (hw["chosen_label"], names))


def rule_vram(manifest, hw):
    title = "GPU memory (VRAM)"
    req = manifest["requirements"]
    need, rec = req["min_vram_gb"], req["recommended_vram_gb"]
    if hw["chosen_backend"] is None:
        return rule("vram", title, "skip", "Unknown until a render device is detected.")
    if hw["chosen_backend"] == "CPU":
        return rule("vram", title, "skip", "Not used: rendering on the CPU.")
    v = hw["vram_gb"]
    src = ", ".join(sorted({g["vram_source"] for g in hw["gpus"] if g.get("vram_source")}))
    if not need and not rec:
        return rule("vram", title, "pass", "No VRAM requirement in project.json. %s"
                    % ("VRAM %.1f GB (%s)." % (v, src) if v else "VRAM unknown."))
    if v is None:
        return rule("vram", title, "warn",
                    "VRAM unknown on this PC; the project needs %.0f GB." % (need or rec),
                    "Enter your GPU's VRAM in Settings (for example 20 for an RX 7900 XT).")
    if need and v < need:
        return rule("vram", title, "fail", "%.1f GB VRAM (%s); the project needs %.0f GB." % (v, src, need),
                    "Use a GPU with more memory, lower the samples/resolution in the scene, or render on CPU (Device: CPU).")
    if hw.get("vram_unified"):
        msg = "%.1f GB unified memory (shared with the system); the project needs %.0f GB." % (v, need or rec)
        if rec and v < rec + 4:
            return rule("vram", title, "warn", msg + " Close other apps while rendering.")
        return rule("vram", title, "pass", msg)
    if rec and v < rec:
        return rule("vram", title, "warn", "%.1f GB VRAM (%s); %.0f GB recommended. It may run out on heavy shots."
                    % (v, src, rec))
    return rule("vram", title, "pass", "%.1f GB VRAM (%s); needs %.0f GB." % (v, src, need or rec))


def rule_ram(manifest, hw):
    title = "System memory (RAM)"
    need = manifest["requirements"]["min_ram_gb"]
    total, free = hw["ram_total_gb"], hw["ram_free_gb"]
    if total is None:
        return rule("ram", title, "warn", "RAM could not be read on this PC.")
    if not need:
        return rule("ram", title, "pass", "%.0f GB RAM; no minimum in project.json." % total)
    if total < need:
        return rule("ram", title, "fail", "%.0f GB RAM; the project needs %.0f GB." % (total, need),
                    "Use a PC with more RAM.")
    if free is not None and free < need:
        return rule("ram", title, "warn", "%.0f GB RAM, but only %.1f GB free now; the project needs %.0f GB."
                    % (total, free, need), "Close other programs before rendering.")
    return rule("ram", title, "pass", "%.0f GB RAM (%.1f GB free); needs %.0f GB." % (total, free or 0, need))


def rule_disk(manifest, hw, total_frames):
    title = "Free disk space for frames"
    d = hw["disks"]["output"]
    est = manifest["requirements"]["est_disk_gb"]
    note = ""
    if not est:
        est = estimated_disk_gb(manifest, total_frames)
        note = " (estimated from resolution)"
    free = util.gb(d["free"])
    if free is None:
        return rule("disk", title, "warn", "Free space on the output drive could not be read.")
    if free < est:
        return rule("disk", title, "fail", "%.1f GB free on the output drive (%s); about %.1f GB needed%s."
                    % (free, d["mount"], est, note), "Free up space or point output_dir to a bigger drive.")
    return rule("disk", title, "pass", "%.0f GB free on %s; about %.1f GB needed%s." % (free, d["mount"], est, note))


def rule_blender_check(manifest, check, blender, integrity):
    title = "Opens in Blender, nothing missing"
    if not blender.get("selected"):
        return rule("blender_check", title, "skip", "Not run: Blender not found.")
    if check is None:
        return rule("blender_check", title, "pending", "Checking the .blend in Blender...")
    if check.get("state") == "error":
        return rule("blender_check", title, "fail", "Blender could not check the file: %s" % check.get("error"),
                    "See the log in logs/. A damaged .blend must be copied again.")
    r = check["result"]
    if not r.get("ok"):
        return rule("blender_check", title, "fail", "The check script failed inside Blender.",
                    "See the log in logs/.", [r.get("error", "")])
    fails, warns = [], []
    im = r["images"]
    if im["missing_count"]:
        fails.append("%d missing image/texture file%s (these render magenta). First: %s"
                     % (im["missing_count"], "" if im["missing_count"] == 1 else "s",
                        ", ".join("%s (%s)" % (x["name"], x["path"]) for x in im["missing"][:3])))
    for key, label in (("libraries", "linked .blend librar%s"), ("fonts", "font%s"), ("volumes", "volume file%s"),
                       ("cache_files", "cache file%s")):
        n = r[key]["missing_count"]
        if n:
            suffix = ("y" if n == 1 else "ies") if key == "libraries" else ("" if n == 1 else "s")
            fails.append("%d missing %s: %s" % (n, label % suffix, ", ".join(x["path"] for x in r[key]["missing"][:3])))
    for fx in r.get("fixes", []):
        if not fx["ok"]:
            fails.append("Fix script %s is broken: %s" % (fx["file"], fx["error"]))
    for s in r.get("shots", []):
        for p in s["problems"]:
            fails.append("Shot %s: %s" % (s["id"], p))
    n_abs = sum(r[k].get("absolute_count", 0) for k in ("images", "libraries", "fonts", "sounds", "movieclips", "volumes", "cache_files"))
    if n_abs:
        warns.append("%d file path%s %s absolute and will break on another PC." % (n_abs, "" if n_abs == 1 else "s", "is" if n_abs == 1 else "are"))
    if im.get("missing_unused_count"):
        warns.append("%d unused image%s missing (not used by anything, harmless)." % (im["missing_unused_count"], "" if im["missing_unused_count"] == 1 else "s"))
    for key, label in (("sounds", "sound"), ("movieclips", "movie clip")):
        n = r[key]["missing_count"]
        if n:
            warns.append("%d missing %s file%s (not needed for frames)." % (n, label, "" if n == 1 else "s"))
    if r.get("other_missing_count"):
        warns.append("%d other referenced file%s missing: %s" % (r["other_missing_count"], "" if r["other_missing_count"] == 1 else "s",
                                                                  ", ".join(r["other_missing"][:3])))
    if r.get("engine") != "CYCLES":
        warns.append("Scene engine is %s; RenderHub renders with Cycles only. Check test frames." % r.get("engine"))
    expect = (integrity or {}).get("image_count")
    if isinstance(expect, int) and expect != im["total"] - im["generated"]:
        warns.append("Package recorded %d images at export, Blender sees %d now." % (expect, im["total"] - im["generated"]))
    details = fails + warns
    n_img = im["total"] - im["generated"]
    summary = "%d image%s (%d packed), %d objects, %d actions, file saved by Blender %s." % (
        n_img, "" if n_img == 1 else "s", im["packed"], r.get("objects", 0), r.get("actions", 0), r.get("file_version"))
    if fails:
        return rule("blender_check", title, "fail", fails[0],
                    "Re-export the package so all files sit in assets/ with relative paths, or copy the missing files.",
                    details + [summary])
    if warns:
        return rule("blender_check", title, "warn", warns[0], None, details + [summary])
    return rule("blender_check", title, "pass", summary)


def rule_video(entry, manifest, hw):
    """The finished video (sound, captions, text) needs ffmpeg with drawtext. Frames render without it,
    so problems here are warnings: RenderHub then makes a silent video."""
    from . import assemble
    title = "Finished video (sound, captions)"
    v = manifest.get("video")
    if not v:
        return rule("video", title, "skip", "No video section in project.json: a silent MP4 is made from the frames.")
    problems, notes = assemble.check_inputs(entry["path"], v)
    ff = hw.get("ffmpeg") or {"ok": False, "problem": "ffmpeg not checked"}
    if problems:
        return rule("video", title, "warn", "Video section problem: %s. A silent video is made instead." % problems[0],
                    "Re-export the package with its audio, captions and font.", problems + notes)
    if not ff["ok"]:
        return rule("video", title, "warn", ff["problem"] + " Until then a silent video is made instead.",
                    "Install ffmpeg, or set its path in Settings.")
    au = v.get("audio") or {}
    parts = [n for n in notes if n.endswith("captions")]
    parts.append("voice" + (" + sfx" if au.get("sfx") else "") if au.get("voice") else "no voice")
    if v.get("overlays"):
        parts.append("%d on-screen texts" % len(v["overlays"]))
    return rule("video", title, "pass", "ffmpeg %s ready: %s, to %s." % (ff["version"], ", ".join(parts),
                                                                       assemble.output_rel(manifest, entry["id"])))


def rule_drives(hw):
    title = "Drives for projects and output"
    warns = []
    seen = set()
    for label, d in (("projects", hw["disks"]["projects"]), ("output", hw["disks"]["output"])):
        if d["mount"] in seen:
            continue
        seen.add(d["mount"])
        if d["kind"] == "HDD":
            warns.append("The %s folder is on a hard disk (%s); renders and checks will be slower. Use the SSD."
                         % (label, d["mount"]))
        if d["free_pct"] is not None and d["free_pct"] < 10:
            warns.append("The %s drive %s is nearly full (%.0f%% free)." % (label, d["mount"], d["free_pct"]))
    if warns:
        return rule("drives", title, "warn", warns[0], "Move projects/ and output/ to a fast SSD with free space.", warns)
    kinds = sorted({hw["disks"][k]["kind"] or "type unknown" for k in ("projects", "output")})
    return rule("drives", title, "pass", "Drive: %s, enough free space." % ", ".join(kinds))


def evaluate(entry, hw, blender):
    """Full checklist for one scanned project entry."""
    pkg = entry.get("package")
    rules = list(pkg["rules"]) if pkg else []
    if pkg and pkg["manifest"] and not pkg["invalid"]:
        m = pkg["manifest"]
        integ = next((r for r in pkg["rules"] if r["id"] == "integrity"), None)
        rules = [r for r in rules if r["id"] != "integrity"]
        rules += [rule_blender(m, blender), rule_gpu(m, hw), rule_vram(m, hw), rule_ram(m, hw),
                  rule_disk(m, hw, pkg["total_frames"])]
        if integ:
            rules.append(integ)
        rules += [rule_blender_check(m, entry.get("blender_check"), blender, m.get("integrity")), rule_video(entry, m, hw),
                  rule_drives(hw)]
    return rules


def overall(entry, rules):
    """(status, text). status is one of copying, checking, invalid, blocked, warnings, ready."""
    phase = entry.get("phase")
    if phase == "copying":
        return "copying", "Copying..."
    if phase == "extracting":
        return "checking", "Extracting zip..."
    if entry.get("zip_problem"):
        return "invalid", "Invalid"
    if phase in ("waiting", "queued", "validating") and not entry.get("package"):
        return "checking", "Checking..."
    pkg = entry.get("package")
    if pkg and pkg["invalid"]:
        return "invalid", "Invalid"
    st = [r["status"] for r in rules]
    if "fail" in st:
        return "blocked", "Blocked"
    if "pending" in st or phase in ("queued", "validating"):
        return "checking", "Checking..."
    if "warn" in st:
        return "warnings", "Warnings"
    return "ready", "Ready"
