"""plan_shots with one Blender scene per shot (each numbered from frame 1)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import jobs, package  # noqa: E402


def shots(spec):
    """spec: (id, scene_name or None, frame_start, frame_end)"""
    return [{"id": i, "scene_name": sc, "frame_start": a, "frame_end": b, "order": n}
            for n, (i, sc, a, b) in enumerate(spec)]


class PerScene(unittest.TestCase):
    def test_a_different_scenes_render_every_frame(self):
        planned, notes = package.plan_shots(shots([("S01", "S01", 1, 20), ("S02", "S02", 1, 20)]))
        self.assertEqual([s["render_frames"] for s in planned], [20, 20])
        self.assertEqual(sum(s["render_frames"] for s in planned), 40)
        self.assertEqual([(s["render_start"], s["render_end"]) for s in planned], [(1, 20), (1, 20)])
        self.assertEqual(notes, [])

    def test_b_same_scene_shared_boundary_renders_once(self):
        planned, notes = package.plan_shots(shots([("A", "Main", 0, 72), ("B", "Main", 72, 240)]))
        self.assertEqual(planned[1]["render_start"], 73)
        self.assertEqual(sum(s["render_frames"] for s in planned), 241)
        self.assertEqual(len(notes), 1)
        self.assertIn("rendered once, by A", notes[0])

    def test_c_mixed_default_pair_and_two_scenes(self):
        planned, notes = package.plan_shots(shots([
            ("D1", None, 1, 30), ("X", "SceneX", 1, 10), ("D2", "", 21, 40), ("Y", "SceneY", 1, 10)]))
        by = {s["id"]: s for s in planned}
        self.assertEqual([s["id"] for s in planned], ["D1", "X", "D2", "Y"])  # original order kept
        self.assertEqual((by["D2"]["render_start"], by["D2"]["render_frames"]), (31, 10))  # None and "" = one scene
        self.assertEqual(by["X"]["render_frames"], 10)
        self.assertEqual(by["Y"]["render_frames"], 10)
        self.assertEqual(len(notes), 1)
        self.assertIn("D2 overlaps earlier shots by 10 frames (21-30)", notes[0])

    def test_no_gap_notes_between_scenes(self):
        _, notes = package.plan_shots(shots([("A", "S1", 1, 5), ("B", "S2", 50, 60)]))
        self.assertEqual(notes, [])

    def test_samples_like_layout_twelve_scenes(self):
        planned, notes = package.plan_shots(shots([("S%02d" % i, "S%02d" % i, 1, 20) for i in range(1, 13)]))
        self.assertEqual(sum(s["render_frames"] for s in planned), 240)
        self.assertEqual(notes, [])

    def test_video_and_test_frames_follow_list_order(self):
        planned, _ = package.plan_shots(shots([("S01", "S01", 1, 20), ("S02", "S02", 1, 20), ("S03", "S03", 1, 20)]))
        self.assertEqual(jobs.pick_test_frames(planned, 3), [("S01", 1), ("S02", 10), ("S03", 20)])


if __name__ == "__main__":
    unittest.main()
