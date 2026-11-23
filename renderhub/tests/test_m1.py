"""M1 unit tests (no Blender needed).   python -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import package, requirements  # noqa: E402
from server.scanner import signature  # noqa: E402

FROZEN = [("S01", 0, 72), ("S02", 72, 240), ("S03", 240, 507), ("S04", 507, 700), ("S05", 700, 900),
          ("S06", 900, 1050), ("S07", 1050, 1200), ("S08", 1200, 1350), ("S09", 1350, 1500),
          ("S10", 1500, 1653), ("S11", 1653, 1800)]


def shots(spec):
    return [{"id": i, "frame_start": a, "frame_end": b, "order": n} for n, (i, a, b) in enumerate(spec)]


class ShotPlan(unittest.TestCase):
    def test_frozen_boundaries_render_each_frame_once(self):
        planned, notes = package.plan_shots(shots(FROZEN))
        self.assertEqual(sum(s["render_frames"] for s in planned), 1801)  # frames 0..1800
        self.assertEqual(planned[1]["render_start"], 73)
        self.assertEqual(len(notes), 10)
        self.assertIn("rendered once, by S01", notes[0])

    def test_gap_and_full_overlap(self):
        planned, notes = package.plan_shots(shots([("A", 1, 10), ("B", 3, 8), ("C", 20, 30)]))
        self.assertEqual(planned[1]["render_frames"], 0)
        self.assertTrue(any("not in any shot" in n for n in notes))
        self.assertTrue(any("fully inside" in n for n in notes))

    def test_clean_ranges_have_no_notes(self):
        _, notes = package.plan_shots(shots([("A", 1, 6), ("B", 7, 12)]))
        self.assertEqual(notes, [])


class Zips(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, entries, name="p.zip"):
        z = self.d / name
        with zipfile.ZipFile(z, "w") as zf:
            for n, data in entries.items():
                zf.writestr(n, data)
        return z

    def test_zip_slip_rejected(self):
        z = self.make({"p/project.json": "{}", "p/../../evil.txt": "x"})
        with self.assertRaises(package.ZipProblem) as cm:
            package.inspect_zip(z, 10 ** 9)
        self.assertIn("unsafe path", str(cm.exception))

    def test_absolute_path_rejected(self):
        z = self.make({"/etc/project.json": "{}"})
        with self.assertRaises(package.ZipProblem):
            package.inspect_zip(z, 10 ** 9)

    def test_truncated_zip(self):
        z = self.make({"p/project.json": "{}" * 5000, "p/scene.blend": os.urandom(50000)})
        z.write_bytes(z.read_bytes()[:30000])
        with self.assertRaises(package.ZipProblem) as cm:
            package.inspect_zip(z, 10 ** 9)
        self.assertIn("truncated", str(cm.exception))

    def test_root_level_and_folder_layouts(self):
        self.assertEqual(package.inspect_zip(self.make({"project.json": "{}"}, "flat.zip"), 10 ** 9)[:2], ("flat", ""))
        self.assertEqual(package.inspect_zip(self.make({"Pkg/project.json": "{}", "Pkg/a.txt": "1"}), 10 ** 9)[:2],
                         ("Pkg", "Pkg/"))

    def test_extract_lands_whole(self):
        z = self.make({"Pkg/project.json": "{}", "Pkg/assets/ü ñ.png": "x"})
        name, prefix, _ = package.inspect_zip(z, 10 ** 9)
        package.extract_zip(z, prefix, self.d / name, self.d / ".renderhub-extract-Pkg")
        self.assertTrue((self.d / "Pkg" / "assets" / "ü ñ.png").exists())
        self.assertFalse((self.d / ".renderhub-extract-Pkg").exists())

    def test_size_cap(self):
        z = self.make({"P/project.json": "0" * 5000})
        with self.assertRaises(package.ZipProblem):
            package.inspect_zip(z, 100)


class Blend(unittest.TestCase):
    def test_headers(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.blend"
            p.write_bytes(b"")
            self.assertIn("0 bytes", package.blend_header_problem(p))
            p.write_bytes(b"BLENDER-v405" + b"\0" * 1000)
            self.assertIn("truncated", package.blend_header_problem(p))
            p.write_bytes(b"BLENDER-v405" + b"\0" * 1000 + b"ENDB" + b"\0" * 20)
            self.assertIsNone(package.blend_header_problem(p))
            p.write_bytes(package.ZSTD_MAGIC + b"\0" * 100)
            self.assertIsNone(package.blend_header_problem(p))


class Manifest(unittest.TestCase):
    def test_paths_must_stay_inside(self):
        for bad in ("../x.blend", "/abs/x.blend", "C:\\x.blend", "a/../../b"):
            self.assertFalse(package.safe_relpath(bad), bad)
        self.assertTrue(package.safe_relpath("sub/scene.blend"))

    def test_newer_schema_is_invalid(self):
        m, errs, _ = package.normalize({"schema_version": 2, "name": "x", "shots": [{"id": "A", "frame_start": 1, "frame_end": 2}]})
        self.assertTrue(any("newer" in e for e in errs))

    def test_version_compare(self):
        self.assertLess(requirements._ver_tuple("4.2.3"), requirements._ver_tuple("4.10"))
        self.assertEqual(requirements._ver_tuple("5.3.0 LTS"), (5, 3, 0))


class Settle(unittest.TestCase):
    def test_signature_changes_while_copying(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "a.bin").write_bytes(b"1" * 10)
            s1 = signature(d, "folder")
            time.sleep(0.01)
            with open(d / "a.bin", "ab") as f:
                f.write(b"2")
            self.assertNotEqual(s1, signature(d, "folder"))


if __name__ == "__main__":
    unittest.main()
