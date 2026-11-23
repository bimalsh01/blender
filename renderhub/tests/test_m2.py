"""M2 unit tests (no Blender needed).   python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import jobs, package  # noqa: E402
from tests.test_m1 import FROZEN, shots  # noqa: E402


def planned(spec):
    s, _ = package.plan_shots(shots(spec))
    return s


class TestFrames(unittest.TestCase):
    def test_three_frames_across_frozen(self):
        picks = jobs.pick_test_frames(planned(FROZEN), 3)
        self.assertEqual(picks, [("S01", 0), ("S06", 975), ("S11", 1800)])

    def test_one_shot_gets_first_middle_last(self):
        self.assertEqual(jobs.pick_test_frames(planned([("A", 1, 9)]), 3), [("A", 1), ("A", 5), ("A", 9)])

    def test_respects_rendered_range_not_shared_boundary(self):
        picks = jobs.pick_test_frames(planned([("A", 0, 72), ("B", 72, 240)]), 2)
        self.assertEqual(picks, [("A", 0), ("B", 156)])  # B renders 73-240

    def test_empty(self):
        self.assertEqual(jobs.pick_test_frames([], 3), [])


class Estimates(unittest.TestCase):
    def test_measured_beats_test_after_three_frames(self):
        with tempfile.TemporaryDirectory() as t:
            b = jobs.Benchmarks(Path(t) / "b.json")
            b.set_test("k", "p", {"frames": [{"shot": "S01", "frame": 1, "seconds": 10.0}]})
            self.assertEqual(b.spf("k", "S01"), (10.0, "test"))
            for _ in range(3):
                b.add_measured("k", "p", "S01", 4.0)
            self.assertEqual(b.spf("k", "S01"), (4.0, "measured"))
            self.assertEqual(b.spf("k", "S99"), (None, None))
            self.assertEqual(b.spf("other", "S01"), (None, None))

    def test_durations(self):
        self.assertEqual(jobs.fmt_duration(42), "42s")
        self.assertEqual(jobs.fmt_duration(125), "2m 05s")
        self.assertEqual(jobs.fmt_duration(3 * 3600 + 7 * 60), "3h 07m")

    def test_oom_patterns(self):
        for line in ("CUDA error: Out of memory in cuMemAlloc", "hipErrorOutOfMemory", "System is out of GPU memory",
                     "Metal: MTLCommandBufferErrorOutOfMemory"):
            self.assertTrue(jobs.OOM_RE.search(line), line)
        self.assertFalse(jobs.OOM_RE.search("Fra:12 Mem:1.2G (Peak 2.1G)"))


if __name__ == "__main__":
    unittest.main()
