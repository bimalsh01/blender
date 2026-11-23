"""Project package checks that need no Blender: project.json, the .blend file,
checksums, shot ranges, and safe zip extraction. Also used by the exporter."""

import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from . import util

SUPPORTED_SCHEMA = 1
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"

RENDER_DEFAULTS = {"fps": 30, "resolution_x": 1080, "resolution_y": 1920, "format": "PNG",
                   "color_depth": "8", "samples": 128, "denoise": True, "persistent_data": False}


def rule(rid, title, status, message, fix=None, details=None):
    """One checklist line. status: pass | warn | fail | skip | pending."""
    return {"id": rid, "title": title, "status": status, "message": message,
            "fix": fix, "details": details or []}


def safe_relpath(rel):
    """True when rel is a relative path that stays inside the package."""
    if not isinstance(rel, str) or not rel.strip():
        return False
    r = rel.replace("\\", "/")
    if r.startswith("/") or re.match(r"^[A-Za-z]:", r):
        return False
    return ".." not in PurePosixPath(r).parts


def pkg_path(folder, rel):
    return Path(folder).joinpath(*PurePosixPath(rel.replace("\\", "/")).parts)


def blend_header_problem(path):
    """Return None if the file looks like a complete .blend, else a sentence."""
    try:
        size = os.path.getsize(util.longpath(path))
    except OSError:
        return "file is missing"
    if size == 0:
        return "file is empty (0 bytes); the copy or save did not finish"
    with open(util.longpath(path), "rb") as f:
        head = f.read(12)
        if head.startswith(b"BLENDER"):
            # Uncompressed .blend files end with an ENDB block.
            f.seek(max(0, size - 32))
            if b"ENDB" not in f.read():
                return "file is truncated (no end marker); copy it again"
            return None
        if head.startswith(ZSTD_MAGIC) or head.startswith(GZIP_MAGIC):
            return None  # compressed: completeness is checked by checksum and by opening it in Blender
    return "not a Blender file (unknown header)"


# ---------------------------------------------------------------- manifest

def load_manifest(folder):
    """Returns (manifest dict or None, list of structure errors)."""
    pj = Path(folder) / "project.json"
    if not pj.exists():
        return None, ["project.json is missing. Every package needs one at its top level."]
    try:
        size = pj.stat().st_size
    except OSError as e:
        return None, ["project.json cannot be read: %s" % e]
    if size == 0:
        return None, ["project.json is empty (0 bytes)."]
    import json
    try:
        with open(util.longpath(pj), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except UnicodeDecodeError:
        return None, ["project.json is not UTF-8 text."]
    except ValueError as e:
        return None, ["project.json is not valid JSON: %s" % e]
    if not isinstance(data, dict):
        return None, ["project.json must contain a JSON object."]
    return data, []


def normalize(m):
    """Fill defaults and check types. Returns (manifest, errors, warnings)."""
    errors, warnings = [], []
    out = dict(m)
    sv = m.get("schema_version")
    if not isinstance(sv, int):
        errors.append("schema_version must be a whole number (1).")
    elif sv > SUPPORTED_SCHEMA:
        errors.append("schema_version %d is newer than this RenderHub understands (%d). Update RenderHub."
                      % (sv, SUPPORTED_SCHEMA))
    name = m.get("name")
    if not isinstance(name, str) or not name:
        errors.append("name is missing.")
    elif not SLUG_RE.match(name):
        warnings.append("name '%s' is not folder-safe (letters, digits, . _ - only)." % name)
    out["title"] = m.get("title") or name or "?"
    bf = m.get("blend_file", "scene.blend")
    if not safe_relpath(bf):
        errors.append("blend_file must be a relative path inside the package, got %r." % (bf,))
    out["blend_file"] = bf

    b = m.get("blender") or {}
    out["blender"] = {"min_version": str(b.get("min_version") or ""), "built_with": str(b.get("built_with") or "")}

    r = dict(RENDER_DEFAULTS)
    r.update(m.get("render") or {})
    for k in ("fps", "resolution_x", "resolution_y", "samples"):
        try:
            r[k] = int(r[k])
            if r[k] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("render.%s must be a positive whole number." % k)
    out["render"] = r

    req = m.get("requirements") or {}
    q = {}
    for k in ("min_vram_gb", "min_ram_gb", "recommended_vram_gb", "est_disk_gb"):
        v = req.get(k)
        if v in (None, ""):
            q[k] = None
            continue
        try:
            q[k] = float(v)
        except (TypeError, ValueError):
            errors.append("requirements.%s must be a number." % k)
            q[k] = None
    q["gpu_required"] = bool(req.get("gpu_required", False))
    out["requirements"] = q

    shots = m.get("shots")
    norm_shots = []
    if not isinstance(shots, list) or not shots:
        errors.append("shots must be a non-empty list.")
    else:
        ids = set()
        for i, s in enumerate(shots):
            if not isinstance(s, dict):
                errors.append("shots[%d] is not an object." % i)
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                errors.append("shots[%d] has no id." % i)
                continue
            if sid in ids:
                errors.append("shot id %s is used twice." % sid)
            ids.add(sid)
            try:
                fs, fe = int(s.get("frame_start")), int(s.get("frame_end"))
            except (TypeError, ValueError):
                errors.append("shot %s needs whole-number frame_start and frame_end." % sid)
                continue
            if fe < fs:
                errors.append("shot %s ends (%d) before it starts (%d)." % (sid, fe, fs))
                continue
            fixes = s.get("fixes") or []
            if not isinstance(fixes, list):
                errors.append("shot %s: fixes must be a list of file names." % sid)
                fixes = []
            samples = s.get("samples")
            if samples is not None:
                try:
                    samples = int(samples)
                except (TypeError, ValueError):
                    errors.append("shot %s: samples must be a whole number." % sid)
                    samples = None
            norm_shots.append({"id": sid, "frame_start": fs, "frame_end": fe,
                               "scene_name": s.get("scene_name"), "camera": s.get("camera"),
                               "samples": samples, "fixes": [str(x) for x in fixes],
                               "notes": s.get("notes") or "", "order": i})
    out["shots"] = norm_shots
    gf = m.get("global_fixes") or []
    if not isinstance(gf, list):
        errors.append("global_fixes must be a list of file names.")
        gf = []
    out["global_fixes"] = [str(x) for x in gf]
    out["integrity"] = m.get("integrity") if isinstance(m.get("integrity"), dict) else None
    out["post"] = m.get("post") if isinstance(m.get("post"), list) else []
    return out, errors, warnings


def plan_shots(shots):
    """Decide which frames each shot renders.

    Frame numbers only mean something inside one Blender scene, so shots are planned per
    scene_name (a missing or empty scene_name is the "default scene"). Within one scene the rule
    is: a frame that belongs to several shots is rendered once, by the earlier shot (lower
    frame_start, then list order). Shots in different scenes never affect each other: every
    shot-per-scene project (each scene numbered from frame 1) renders every frame.
    Returns (shots in their original order, with render_start/render_end/render_frames, notes).
    Notes come per scene, in the order the scenes first appear in the list."""
    notes = []
    groups = {}
    for s in shots:
        groups.setdefault(s.get("scene_name") or "", []).append(s)
    for group in groups.values():
        notes += _plan_one_timeline(group)
    return shots, notes


def _plan_one_timeline(shots):
    """Overlap/gap rule for shots that share one scene's frame numbers (updates them in place)."""
    notes = []
    ordered = sorted(shots, key=lambda s: (s["frame_start"], s["order"]))
    covered_to = None
    prev = None
    for s in ordered:
        start = s["frame_start"]
        if covered_to is not None and start <= covered_to:
            overlap_end = min(covered_to, s["frame_end"])
            n = overlap_end - start + 1
            if n == 1:
                notes.append("%s and %s share frame %d; it is rendered once, by %s, so %s starts at %d."
                             % (prev["id"], s["id"], start, prev["id"], s["id"], start + 1))
            else:
                notes.append("%s overlaps earlier shots by %d frames (%d-%d); those frames are rendered once, "
                             "by the earlier shot." % (s["id"], n, start, overlap_end))
            start = covered_to + 1
        elif covered_to is not None and start > covered_to + 1:
            notes.append("Frames %d-%d are not in any shot (between %s and %s)."
                         % (covered_to + 1, start - 1, prev["id"], s["id"]))
        if start > s["frame_end"]:
            s["render_start"] = s["render_end"] = None
            s["render_frames"] = 0
            notes.append("%s lies fully inside earlier shots; nothing left to render for it." % s["id"])
        else:
            s["render_start"], s["render_end"] = start, s["frame_end"]
            s["render_frames"] = s["frame_end"] - start + 1
        covered_to = s["frame_end"] if covered_to is None else max(covered_to, s["frame_end"])
        prev = s
    return notes


# ---------------------------------------------------------------- integrity

def list_files(folder):
    """All files in a package (relative POSIX paths), skipping hidden/system files."""
    folder = Path(folder)
    out = []
    for root, dirs, files in os.walk(util.longpath(folder)):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fn in files:
            if fn.startswith(".") or fn in ("Thumbs.db", "desktop.ini"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, util.longpath(folder)).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def compute_integrity(folder, blend_rel="scene.blend", image_count=None, linked_count=None):
    """Integrity block for project.json: size and sha256 for scene.blend and every file
    under assets/. Used by the exporter and the test package builder."""
    folder = Path(folder)
    files = {}
    total = 0
    for rel in list_files(folder):
        if rel == blend_rel or rel.startswith("assets/"):
            p = pkg_path(folder, rel)
            size = os.path.getsize(util.longpath(p))
            files[rel] = {"size": size, "sha256": util.sha256_file(p)}
            total += size
    return {"algorithm": "sha256", "image_count": image_count, "linked_library_count": linked_count,
            "file_count": len(files), "total_bytes": total, "files": files}


class HashCache:
    """sha256 per file, reused while size and mtime do not change (state/hashes.json)."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = util.read_json(self.path, {}) or {}
        self.dirty = False

    def get(self, p):
        st = os.stat(util.longpath(p))
        key = os.path.abspath(str(p))
        hit = self.data.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
        h = util.sha256_file(p)
        self.data[key] = [st.st_size, st.st_mtime_ns, h]
        self.dirty = True
        return h

    def save(self):
        if self.dirty:
            try:
                util.write_json_atomic(self.path, self.data)
                self.dirty = False
            except OSError:
                pass


def check_integrity(folder, manifest, hashes):
    """Returns a rule dict. fail = a listed file is missing, has the wrong size or checksum."""
    integ = manifest.get("integrity")
    title = "Package files and checksums"
    if not integ or not isinstance(integ.get("files"), dict):
        return rule("integrity", title, "warn", "No checksums in project.json, so a bad copy cannot be detected.",
                    "Re-export the package with the RenderHub exporter.")
    missing, wrong_size, wrong_sum, unsafe = [], [], [], []
    for rel, meta in sorted(integ["files"].items()):
        if not safe_relpath(rel):
            unsafe.append(rel)
            continue
        p = pkg_path(folder, rel)
        try:
            size = os.path.getsize(util.longpath(p))
        except OSError:
            missing.append(rel)
            continue
        if isinstance(meta, dict) and meta.get("size") is not None and size != meta.get("size"):
            wrong_size.append("%s (%s, expected %s)" % (rel, util.human_bytes(size), util.human_bytes(meta["size"])))
            continue
        want = meta.get("sha256") if isinstance(meta, dict) else None
        if want and hashes.get(p) != want:
            wrong_sum.append(rel)
    details = (["Missing: " + x for x in missing] + ["Wrong size: " + x for x in wrong_size]
               + ["Checksum mismatch: " + x for x in wrong_sum] + ["Unsafe path in manifest: " + x for x in unsafe])
    if details:
        n = len(details)
        first = details[0]
        return rule("integrity", title, "fail",
                    "%d file problem%s. First: %s" % (n, "" if n == 1 else "s", first),
                    "Copy the package again from the source PC (or re-extract the zip).", details)
    return rule("integrity", title, "pass", "%d files present, checksums match (%s)."
                % (len(integ["files"]), util.human_bytes(integ.get("total_bytes"))))


def validate_package(folder, hashes):
    """All checks that need no Blender. Returns dict with manifest, shots, rules, invalid flag."""
    folder = Path(folder)
    result = {"manifest": None, "shots": [], "rules": [], "invalid": False, "total_frames": 0}
    m, errs = load_manifest(folder)
    title = "Package structure"
    if m is None:
        result["rules"].append(rule("package", title, "fail", errs[0],
                                    "Add project.json (the exporter writes it), or copy the full package folder."))
        result["invalid"] = True
        return result
    m, errs, warns = normalize(m)
    if not errs:
        blend = pkg_path(folder, m["blend_file"])
        prob = blend_header_problem(blend)
        if prob:
            errs.append("%s: %s." % (m["blend_file"], prob))
        for fx in m["global_fixes"] + [f for s in m["shots"] for f in s["fixes"]]:
            if not safe_relpath(fx):
                errs.append("fix script path %r is not a relative path inside the package." % fx)
            elif not fix_path(folder, fx).is_file():
                errs.append("fix script %s is listed but missing from fixes/." % fx)
    result["manifest"] = m
    if errs:
        result["rules"].append(rule("package", title, "fail", errs[0],
                                    "Re-export or re-copy the package.", errs + warns))
        result["invalid"] = True
        return result
    if warns:
        result["rules"].append(rule("package", title, "warn", warns[0], None, warns))
    else:
        result["rules"].append(rule("package", title, "pass", "project.json and %s look complete." % m["blend_file"]))

    shots, notes = plan_shots(m["shots"])
    result["shots"] = shots
    result["total_frames"] = sum(s["render_frames"] for s in shots)
    if notes:
        result["rules"].append(rule("shots", "Shot frame ranges", "warn", notes[0], None, notes))
    else:
        result["rules"].append(rule("shots", "Shot frame ranges", "pass",
                                    "%d shots, %d frames, no overlaps or gaps." % (len(shots), result["total_frames"])))

    integ_rule = check_integrity(folder, m, hashes)
    result["rules"].append(integ_rule)
    if integ_rule["status"] == "fail":
        result["invalid"] = True
    return result


def fix_path(folder, rel):
    """Fix scripts are listed by name ('S03_denoise_off.py') or as 'fixes/...'."""
    r = rel.replace("\\", "/")
    if not r.startswith("fixes/"):
        r = "fixes/" + r
    return pkg_path(folder, r)


# ---------------------------------------------------------------- zips

class ZipProblem(Exception):
    pass


def inspect_zip(zip_path, max_bytes):
    """Check a zip without extracting. Returns (package folder name, prefix inside zip,
    total uncompressed bytes). Raises ZipProblem with a plain sentence."""
    try:
        zf = zipfile.ZipFile(util.longpath(zip_path))
    except zipfile.BadZipFile:
        raise ZipProblem("The zip is damaged or incomplete (truncated). Copy it again.")
    except OSError as e:
        raise ZipProblem("The zip cannot be opened: %s" % e)
    with zf:
        infos = zf.infolist()
        if not infos:
            raise ZipProblem("The zip is empty.")
        total = 0
        names = []
        for zi in infos:
            n = zi.filename.replace("\\", "/")
            if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in PurePosixPath(n).parts:
                raise ZipProblem("The zip contains an unsafe path (%s). Not extracted." % zi.filename)
            mode = (zi.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ZipProblem("The zip contains a symbolic link (%s). Not extracted." % zi.filename)
            if n.startswith("__MACOSX/"):
                continue
            total += zi.file_size
            if zi.compress_size and zi.file_size / zi.compress_size > 2000:
                raise ZipProblem("The zip has an implausible compression ratio (%s). Not extracted." % zi.filename)
            names.append(n)
        if total > max_bytes:
            raise ZipProblem("The zip would extract to %s, above the %s limit (max_zip_extract_gb)."
                             % (util.human_bytes(total), util.human_bytes(max_bytes)))
        if "project.json" in names:
            prefix, name = "", Path(zip_path).stem
        else:
            tops = {n.split("/", 1)[0] for n in names if "/" in n}
            tops_files = [n for n in names if "/" not in n and n]
            cand = [t for t in tops if t + "/project.json" in names]
            if len(cand) == 1 and not tops_files and len(tops) == 1:
                prefix, name = cand[0] + "/", cand[0]
            elif cand:
                raise ZipProblem("The zip holds several folders; put exactly one package folder in it.")
            else:
                raise ZipProblem("The zip has no project.json (expected <name>/project.json or project.json at the top).")
        bad = zf.testzip()
        if bad:
            raise ZipProblem("The zip is damaged: %s fails its CRC check. Copy it again." % bad)
    return name, prefix, total


def extract_zip(zip_path, prefix, target, tmp_dir):
    """Extract to a temporary folder, then rename into place, so a half-extracted
    package never appears in projects/."""
    target = Path(target)
    tmp = Path(tmp_dir)
    if tmp.exists():
        shutil.rmtree(util.longpath(tmp), ignore_errors=True)
    tmp.mkdir(parents=True)
    root = os.path.realpath(util.longpath(tmp))
    with zipfile.ZipFile(util.longpath(zip_path)) as zf:
        for zi in zf.infolist():
            n = zi.filename.replace("\\", "/")
            if n.startswith("__MACOSX/") or not n.startswith(prefix):
                continue
            rel = n[len(prefix):]
            if not rel:
                continue
            dest = os.path.realpath(os.path.join(root, *PurePosixPath(rel).parts))
            if not (dest == root or dest.startswith(root + os.sep)):
                raise ZipProblem("Unsafe path in zip: %s" % zi.filename)
            if n.endswith("/"):
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(zi) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
    if target.exists():
        raise ZipProblem("projects/%s appeared while extracting; nothing was overwritten." % target.name)
    os.replace(util.longpath(tmp), util.longpath(target))
