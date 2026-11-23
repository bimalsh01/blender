"""Finished-video command building (no ffmpeg run needed)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assemble  # noqa: E402

FF = {"path": "ffmpeg", "version": "8.1.2", "version_tuple": [8, 1], "ok": True}


def make_pkg(d, with_font=True):
    (d / "post" / "fonts").mkdir(parents=True)
    (d / "audio").mkdir()
    if with_font:
        (d / "post" / "fonts" / "F.ttf").write_bytes(b"font")
    (d / "audio" / "vo.wav").write_bytes(b"wav")
    (d / "post" / "captions.json").write_text(json.dumps([[0, 1, "Hello: it's 100%"], [1, 2, "next"]]))
    return {"render": {"fps": 30}, "video": {
        "font": "post/fonts/F.ttf", "total_seconds": 0.2, "output": "final/x.mp4",
        "captions": {"file": "post/captions.json", "fontsize": 84, "y_fraction": 0.74},
        "audio": {"voice": "audio/vo.wav", "loudnorm": {"I": -14, "TP": -1.5}},
        "overlays": [{"start": 0.1, "end": 0.2, "text": "DAY 2", "color": "0xFF3B30"}],
        "overlay_style": {"fontsize": 120, "fade_in_s": 0.12},
        "dips": [{"a": 0.0, "b": 0.05, "c": 0.06, "d": 0.1}],
        "frame_policy": "missing frame -> repeat last frame of that shot, else black"}}


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.frames = []
        for i in range(1, 7):
            p = self.d / "out" / "S01" / ("frame_%05d.png" % i)
            p.parent.mkdir(parents=True, exist_ok=True)
            if i != 4:  # frame 4 missing
                assemble.write_black_png(str(p), 16, 16)
            self.frames.append(("S01", i, str(p)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_and_filter(self):
        m = make_pkg(self.d / "pkg")
        cmd, info = assemble.build(m, self.d / "pkg", self.frames, self.d / "work", self.d / "o.mp4", FF)
        fc = (self.d / "work" / "filter.txt").read_text()
        self.assertIn("-/filter_complex", cmd)
        self.assertIn("gte(t,0)*lt(t,1)", fc)  # half-open: no double caption at a boundary
        self.assertIn("textfile=t/c0.txt", fc)
        self.assertEqual((self.d / "work" / "t" / "c0.txt").read_text(), "Hello: it's 100%")  # text never escaped
        self.assertIn("loudnorm=I=-14:TP=-1.5", fc)
        self.assertIn("fontcolor=0xFF3B30", fc)
        self.assertIn("overlay=eof_action=pass", fc)
        self.assertEqual(info["captions"], 2)
        self.assertTrue(any("1 missing frames filled (1 repeated" in n for n in info["notes"]))

    def test_old_ffmpeg_uses_script_option(self):
        m = make_pkg(self.d / "pkg")
        cmd, _ = assemble.build(m, self.d / "pkg", self.frames, self.d / "work", self.d / "o.mp4",
                                dict(FF, version_tuple=[6, 1]))
        self.assertIn("-filter_complex_script", cmd)

    def test_inputs_and_output_path(self):
        m = make_pkg(self.d / "pkg", with_font=False)
        problems, _ = assemble.check_inputs(self.d / "pkg", m["video"])
        self.assertTrue(any("font" in p for p in problems))
        self.assertEqual(assemble.output_rel({"video": {"output": "../escape.mp4"}}, "p"), "final/p.mp4")
        self.assertEqual(assemble.output_rel({}, "p"), "final/p.mp4")


if __name__ == "__main__":
    unittest.main()
