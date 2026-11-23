"""Build the tiny synthetic test package and the failure-injection packages.

    python tools/make_test_packages.py              -> writes into <studio>/test_packages/
    python tools/make_test_packages.py --out ../projects

Packages made (all small, render on CPU in seconds):
  renderhub_test            good: 2 shots, 12 frames, a fix script, checksums  -> Ready
  renderhub_test_zip.zip    the same package as a zip                         -> extracted, Ready
  bad_zero_byte_blend       scene.blend is 0 bytes                             -> Invalid
  bad_no_project_json       folder without project.json                        -> Invalid
  bad_checksum              a texture changed after export                     -> Invalid
  bad_missing_texture       the .blend uses a texture that was never shipped   -> Blocked
  bad_broken_fix            a fix script with a syntax error                   -> Blocked
  bad_wrong_version         needs Blender 99.0                                 -> Blocked
  bad_truncated.zip         a zip cut in half during copy                      -> Invalid
  bad_zip_slip.zip          a zip with a ../ path                              -> Invalid (not extracted)
"""

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from server import blender as blender_mod  # noqa: E402
from server import package  # noqa: E402
from server.config import STUDIO_DIR, Config  # noqa: E402


def manifest(name, title, images, blender_version):
    return {
        "schema_version": 1,
        "name": name,
        "title": title,
        "blend_file": "scene.blend",
        "blender": {"min_version": "4.2", "built_with": blender_version},
        "render": {"fps": 30, "resolution_x": 216, "resolution_y": 384, "format": "PNG", "color_depth": "8",
                   "samples": 16, "denoise": False, "persistent_data": False},
        "requirements": {"min_vram_gb": 1, "min_ram_gb": 2, "recommended_vram_gb": 2, "est_disk_gb": 0.05,
                         "gpu_required": False},
        "shots": [
            {"id": "S01", "frame_start": 1, "frame_end": 6, "notes": "cube turns"},
            {"id": "S02", "frame_start": 7, "frame_end": 12, "samples": 8, "fixes": ["S02_lower_samples.py"],
             "notes": "uses a per-shot fix"},
        ],
        "global_fixes": ["global_print.py"],
        "post": [{"name": "Make MP4 from frames", "builtin": "mp4"}],
        "created": {"date": "2026-11-23", "exporter": "make_test_packages 1"},
        "integrity": None,
    }


def write_fixes(folder, broken=False):
    fx = folder / "fixes"
    fx.mkdir(parents=True, exist_ok=True)
    (fx / "global_print.py").write_text("import bpy\nprint('RenderHub test: global fix ran')\n", encoding="utf-8")
    body = "import bpy\nbpy.context.scene.cycles.samples = 8\n"
    if broken:
        body = "import bpy\nbpy.context.scene.cycles.samples = (8\n"  # syntax error on purpose
    (fx / "S02_lower_samples.py").write_text(body, encoding="utf-8")


def finish(folder, m, images):
    m["integrity"] = package.compute_integrity(folder, "scene.blend", image_count=images, linked_count=0)
    (folder / "project.json").write_text(json.dumps(m, indent=2), encoding="utf-8")


def build_scene(blender_exe, state_dir, folder, missing_texture=False):
    folder.mkdir(parents=True, exist_ok=True)
    res = blender_mod.run_script(blender_exe, str(HERE / "blender_make_test_scene.py"),
                                 {"blend": str(folder / "scene.blend"), "missing_texture": missing_texture},
                                 state_dir, timeout=300)
    if not res["result"]:
        raise SystemExit("Blender failed to build the test scene:\n%s" % (res["error"] or res["output_tail"]))
    return res["result"]


def zip_folder(folder, zip_path, arc_root):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                zf.write(p, arc_root + "/" + p.relative_to(folder).as_posix())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(STUDIO_DIR / "test_packages"))
    ap.add_argument("--blender", help="path to Blender (default: same search as RenderHub)")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    cfg = Config()
    state_dir = cfg.folder("state_dir")
    if args.blender:
        exe = blender_mod.resolve_executable(args.blender)
        ver = "?"
    else:
        sel = blender_mod.BlenderLocator(cfg, state_dir).refresh()["selected"]
        if not sel:
            raise SystemExit("Blender not found. Pass --blender <path>.")
        exe, ver = sel["path"], sel["version"]

    def fresh(name):
        p = out / name
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        return p

    # good package
    good = fresh("renderhub_test")
    info = build_scene(exe, state_dir, good)
    ver = info.get("version", ver)
    write_fixes(good)
    finish(good, manifest("renderhub_test", "RenderHub test (tiny)", info["images"], ver), info["images"])

    # zip of a renamed copy (a different name, so it does not clash with the folder)
    zsrc = out / ".tmp_zip_src"
    if zsrc.exists():
        shutil.rmtree(zsrc)
    shutil.copytree(good, zsrc)
    m = json.loads((zsrc / "project.json").read_text(encoding="utf-8"))
    m["name"], m["title"] = "renderhub_test_zip", "RenderHub test (from zip)"
    (zsrc / "project.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    zip_folder(zsrc, fresh("renderhub_test_zip.zip"), "renderhub_test_zip")

    # truncated zip: first 60% of the bytes
    zip_folder(zsrc, fresh("bad_truncated.zip"), "bad_truncated")
    data = (out / "bad_truncated.zip").read_bytes()
    (out / "bad_truncated.zip").write_bytes(data[: int(len(data) * 0.6)])
    shutil.rmtree(zsrc)

    # zip-slip: an entry that tries to write outside the target
    with zipfile.ZipFile(fresh("bad_zip_slip.zip"), "w") as zf:
        zf.writestr("bad_zip_slip/project.json", json.dumps(manifest("bad_zip_slip", "Zip slip", 1, ver)))
        zf.writestr("bad_zip_slip/../../escaped.txt", "this must never be written")

    def variant(name, title, mutate=None, missing_texture=False, broken_fix=False, rebuild=False):
        p = fresh(name)
        if rebuild:
            i = build_scene(exe, state_dir, p, missing_texture=missing_texture)
        else:
            shutil.copytree(good, p)
            i = info
        write_fixes(p, broken=broken_fix)
        m = manifest(name, title, i["images"], ver)
        finish(p, m, i["images"])
        if mutate:
            mutate(p, m)
        return p

    def zero_blend(p, m):
        (p / "scene.blend").write_bytes(b"")

    def no_json(p, m):
        (p / "project.json").unlink()

    def tamper(p, m):
        f = p / "assets" / "textures" / "checker.png"
        b = bytearray(f.read_bytes())
        b[-20] ^= 0xFF
        f.write_bytes(bytes(b))

    def needs_99(p, m):
        m["blender"]["min_version"] = "99.0"
        (p / "project.json").write_text(json.dumps(m, indent=2), encoding="utf-8")

    variant("bad_zero_byte_blend", "Zero-byte .blend", zero_blend)
    variant("bad_no_project_json", "No project.json", no_json)
    variant("bad_checksum", "Checksum mismatch", tamper)
    variant("bad_missing_texture", "Missing texture", missing_texture=True, rebuild=True)
    variant("bad_broken_fix", "Broken fix script", broken_fix=True)
    variant("bad_wrong_version", "Needs Blender 99", needs_99)

    print("Test packages written to %s" % out)
    for p in sorted(out.iterdir()):
        if not p.name.startswith("."):
            print("  " + p.name)


if __name__ == "__main__":
    main()
