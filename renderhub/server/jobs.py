"""Job queue and runner, test renders, progress and time estimates.

A job is one shot (or a chunk of one) rendered by one Blender process running
blender_scripts/render_worker.py. A test job renders a few frames spread across a project.
Jobs persist in state/jobs.json. After a restart, jobs that were running become
"interrupted"; Resume re-queues them and the worker skips frames already on disk.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import assemble, util
from .blender import SCRIPTS_DIR
from .power import SleepGuard

ACTIVE = ("queued", "running")
RESUMABLE = ("interrupted", "cancelled", "failed")
EV = "RENDERHUB_EVENT:"
OOM_RE = re.compile(r"out of (gpu |device )?memory|outofmemory|hipErrorOutOfMemory|CUDA_ERROR_OUT_OF_MEMORY|"
                    r"failed to allocate|MTLCommandBufferErrorOutOfMemory|std::bad_alloc", re.I)
FRAME_NAME = "frame_{frame:05d}.png"
PROGRESS_RE = re.compile(r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|"
                         r"dup_frames|drop_frames|speed|progress)=")
TEST_NAME = "test_{shot}_{frame:05d}.png"
DEFAULT_STARTUP = 15.0


def frame_name(frame):
    return FRAME_NAME.format(frame=frame)


def frame_exists(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def pick_test_frames(shots, count=3):
    """Spread test frames over the project: shots chosen evenly from first to last, and
    within them alternately the first, middle and last frame. Returns [(shot, frame)]."""
    shots = [s for s in shots if s.get("render_frames")]
    if not shots or count < 1:
        return []
    picks = []
    n = len(shots)
    for i in range(count):
        s = shots[0] if count == 1 else shots[int(i * (n - 1) / (count - 1) + 0.5)]
        a, b = s["render_start"], s["render_end"]
        pos = i % 3
        frame = a if pos == 0 else (b if pos == 2 else (a + b) // 2)
        if (s["id"], frame) not in picks:
            picks.append((s["id"], frame))
    return picks


def fmt_duration(sec):
    if sec is None:
        return None
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


# ---------------------------------------------------------------- Windows: kill Blender if RenderHub dies

_win_job = None


def _win_kill_with_us(proc):
    """Put the Blender process in a Windows job object that is closed (and its processes
    killed) when RenderHub exits for any reason. Best effort."""
    global _win_job
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    if _win_job is None:
        h = k32.CreateJobObjectW(None, None)
        if not h:
            raise OSError("CreateJobObject failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(wintypes.HANDLE(h), 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject failed: %d" % ctypes.get_last_error())
        _win_job = h
    if not k32.AssignProcessToJobObject(wintypes.HANDLE(_win_job), wintypes.HANDLE(int(proc._handle))):
        raise OSError("AssignProcessToJobObject failed: %d" % ctypes.get_last_error())


def leftover_worker(pid):
    """True if pid is still a Blender running our render_worker (left from before a restart)."""
    if not pid:
        return False
    if util.IS_WINDOWS:
        ps = "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % int(pid)
        rc, out = util.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20)
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        rc, out = util.run(["ps", "-p", str(pid), "-o", "command="], timeout=10)
    return rc == 0 and "render_worker.py" in (out or "")


def stop_pid(pid):
    if util.IS_WINDOWS:
        util.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ---------------------------------------------------------------- benchmarks (seconds per frame)

class Benchmarks:
    """state/benchmarks.json, keyed by machine + project + settings. Holds test-render
    results and the frame times measured during real renders."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = util.read_json(self.path, None) or {"records": {}, "latest_test": {}}

    def save(self):
        with self.lock:
            util.write_json_atomic(self.path, self.data)

    def record(self, key, project=None):
        with self.lock:
            r = self.data["records"].get(key)
            if r is None:
                r = {"project": project, "created": util.now_iso(), "test": None, "measured": {},
                     "startup": []}
                self.data["records"][key] = r
            return r

    def get(self, key):
        with self.lock:
            return self.data["records"].get(key)

    def add_measured(self, key, project, shot, seconds):
        with self.lock:
            m = self.record(key, project)["measured"].setdefault(shot, [0.0, 0])
            m[0] += seconds
            m[1] += 1

    def add_startup(self, key, project, seconds):
        with self.lock:
            s = self.record(key, project)["startup"]
            s.append(round(seconds, 2))
            del s[:-10]

    def set_test(self, key, project, test):
        with self.lock:
            self.record(key, project)["test"] = test
            self.data["latest_test"][project] = key
        self.save()

    def latest_test(self, project):
        with self.lock:
            key = self.data["latest_test"].get(project)
            r = self.data["records"].get(key) if key else None
            return key, (r or {}).get("test")

    def spf(self, key, shot):
        """Seconds per frame for a shot: measured (3+ frames) beats test frames."""
        r = self.get(key)
        if not r:
            return None, None
        m = r["measured"].get(shot)
        if m and m[1] >= 3:
            return m[0] / m[1], "measured"
        t = [f["seconds"] for f in ((r.get("test") or {}).get("frames") or []) if f["shot"] == shot]
        if t:
            return sum(t) / len(t), "test"
        if m and m[1]:
            return m[0] / m[1], "measured"
        return None, None

    def all_spf(self, key):
        r = self.get(key)
        if not r:
            return []
        vals = [f["seconds"] for f in ((r.get("test") or {}).get("frames") or [])]
        vals += [m[0] / m[1] for m in r["measured"].values() if m[1]]
        return vals

    def startup(self, key):
        r = self.get(key)
        s = (r or {}).get("startup") or []
        return sum(s) / len(s) if s else None


# ---------------------------------------------------------------- the manager

class JobManager:
    def __init__(self, cfg, hw, locator, scanner, state_dir):
        self.cfg, self.hw, self.locator, self.scanner = cfg, hw, locator, scanner
        self.devices = scanner.devices
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "jobs.json"
        self.lock = threading.RLock()
        self.procs = {}
        self.bench = Benchmarks(self.state_dir / "benchmarks.json")
        self.sleep = SleepGuard()
        self._dirty = False
        self._last_save = 0.0
        self._last_disk = 0.0
        data = util.read_json(self.path, None) or {}
        self.jobs = data.get("jobs", [])
        for j in self.jobs:
            if j["state"] == "running":
                j["state"] = "interrupted"
                j["error"] = "RenderHub stopped while this job was running. Frames on disk are kept; Resume continues."
                if leftover_worker(j.get("pid")):
                    util.log("Stopping a leftover Blender (pid %s) from before the restart." % j.get("pid"))
                    stop_pid(j["pid"])
                j["pid"] = None
                j["current"] = None
        for j in self.jobs:
            if j["kind"] == "render" and j["state"] != "done":
                self._count_disk(j)  # the truth is what is on disk
        self._save(force=True)

    # ------------------------------------------------------------ persistence
    def _save(self, force=False):
        with self.lock:
            self._dirty = True
            if not force and time.time() - self._last_save < 1.0:
                return
            util.write_json_atomic(self.path, {"jobs": self.jobs})
            self._dirty = False
            self._last_save = time.time()

    def find(self, job_id):
        with self.lock:
            return next((j for j in self.jobs if j["id"] == job_id), None)

    # ------------------------------------------------------------ keys and folders
    def output_dir(self, project):
        return self.cfg.folder("output_dir") / project

    def settings_key(self, spec, hw=None, blender=None):
        hw = hw or self.hw.summary(self.devices.get(spec["id"]) if self.devices else None)
        blender = blender or self.locator.selected() or {}
        m = spec["manifest"]
        parts = {"host": socket.gethostname(), "project": spec["id"], "package": spec["sig"],
                 "render": m["render"],
                 "shots": [[s["id"], s["samples"], s["fixes"]] for s in m["shots"]],
                 "global_fixes": m["global_fixes"], "backend": hw.get("chosen_backend"),
                 "use_cpu": hw.get("use_cpu"), "threads": hw.get("cpu_threads"),
                 "devices": [g["name"] for g in hw.get("gpus", [])], "blender": blender.get("version")}
        return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]

    # ------------------------------------------------------------ creating jobs
    def _new_job(self, kind, spec, label, tasks, total):
        return {"id": "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4]), "kind": kind,
                "project": spec["id"], "title": spec["manifest"]["title"], "label": label, "tasks": tasks,
                "total": total, "done": 0, "state": "queued", "created": util.now_iso(), "started": None,
                "finished": None, "run_seconds": 0.0, "error": None, "error_kind": None, "returncode": None,
                "pid": None, "current": None, "frame_times": [], "startup_seconds": None, "attempts": 0,
                "bench_key": None, "backend": None, "devices": [], "warnings": []}

    def _check_renderable(self, spec):
        if spec is None:
            raise ValueError("Project not found or not checked yet.")
        if spec["status"] in ("invalid", "blocked"):
            first = spec["problems"][0]["message"] if spec["problems"] else spec["status_text"]
            raise ValueError("%s is %s: %s" % (spec["id"], spec["status_text"], first))
        if spec["status"] == "checking":
            raise ValueError("%s is still being checked; try again in a moment." % spec["id"])

    def create_render_jobs(self, project, shot_ids=None):
        spec = self.scanner.render_spec(project)
        self._check_renderable(spec)
        shots = [s for s in spec["shots"] if s["render_frames"] and (not shot_ids or s["id"] in shot_ids)]
        if not shots:
            raise ValueError("No shots selected (or the selected shots have no frames to render).")
        chunk = self.cfg.get("chunk_size") or 0
        created, skipped = [], []
        with self.lock:
            for s in shots:
                busy = [j for j in self.jobs if j["kind"] == "render" and j["project"] == project
                        and j["tasks"][0]["shot"] == s["id"] and j["state"] in ACTIVE + ("interrupted",)]
                if busy:
                    skipped.append("%s is already %s%s." % (s["id"], busy[0]["state"],
                                                          "; press Resume" if busy[0]["state"] == "interrupted" else ""))
                    continue
                a, b = s["render_start"], s["render_end"]
                d = self.output_dir(project) / s["id"]
                if all(frame_exists(d / frame_name(f)) for f in range(a, b + 1)):
                    skipped.append("%s: all %d frames are already on disk." % (s["id"], b - a + 1))
                    continue
                step = chunk if chunk > 0 else (b - a + 1)
                for start in range(a, b + 1, step):
                    end = min(b, start + step - 1)
                    label = s["id"] if (start, end) == (a, b) else "%s (frames %d-%d)" % (s["id"], start, end)
                    task = {"shot": s["id"], "range": [start, end], "name": FRAME_NAME}
                    job = self._new_job("render", spec, label, [task], end - start + 1)
                    self.jobs.append(job)
                    created.append(job["id"])
            self._save(force=True)
        return {"created": created, "skipped": skipped}

    def video_path(self, project):
        """The finished video: video.output from project.json, else final/<project>.mp4."""
        rel = assemble.output_rel(self.scanner.manifest(project) or {}, project)
        return self.output_dir(project).joinpath(*rel.split("/"))

    def ffmpeg_info(self):
        return assemble.find_ffmpeg(self.cfg.get("ffmpeg_path"))

    def video_engine(self, spec):
        """("ffmpeg", None) for the finished video, or ("blender", why) for the silent fallback."""
        v = spec["manifest"].get("video")
        if not v:
            return "blender", None
        ff = self.ffmpeg_info()
        if not ff["ok"]:
            return "blender", "Silent video only: " + ff["problem"]
        problems, _ = assemble.check_inputs(spec["path"], v)
        if problems:
            return "blender", "Silent video only, the video section has problems: " + "; ".join(problems)
        return "ffmpeg", None

    def project_frames(self, spec):
        """All frames of a project in play order, which is the order of shots in project.json
        (each shot's frames are numbered in its own scene, so frame numbers cannot order shots).
        Returns ([(shot, frame, path)], [missing (shot, frame)])."""
        base = self.output_dir(spec["id"])
        frames, missing = [], []
        for s in sorted(spec["shots"], key=lambda x: x["order"]):
            if not s["render_frames"]:
                continue
            for f in range(s["render_start"], s["render_end"] + 1):
                p = base / s["id"] / frame_name(f)
                frames.append((s["id"], f, p))
                if not frame_exists(p):
                    missing.append((s["id"], f))
        return frames, missing

    def create_video_job(self, project, auto=False):
        spec = self.scanner.render_spec(project)
        if spec is None:
            raise ValueError("Project not found.")
        frames, missing = self.project_frames(spec)
        if not frames:
            raise ValueError("This project has no frames.")
        engine, _ = self.video_engine(spec)
        policy = (spec["manifest"].get("video") or {}).get("frame_policy")
        if missing and not (engine == "ffmpeg" and policy and not auto):
            raise ValueError("%d of %d frames are not rendered yet (first: %s frame %d). Render them first."
                             % (len(missing), len(frames), missing[0][0], missing[0][1]))
        with self.lock:
            if any(j["kind"] == "video" and j["project"] == project and j["state"] in ACTIVE for j in self.jobs):
                raise ValueError("A video for this project is already queued or being made.")
            what = "Finished video (sound, captions)" if engine == "ffmpeg" else "Video, no sound"
            job = self._new_job("video", spec, "%s, %d frames%s" % (what, len(frames), " (automatic)" if auto else ""),
                                [], len(frames))
            self.jobs.append(job)
            self._save(force=True)
        util.log("Video job queued for %s%s" % (project, " automatically" if auto else ""))
        return {"created": [job["id"]]}

    def create_test_job(self, project, count=3):
        spec = self.scanner.render_spec(project)
        self._check_renderable(spec)
        count = max(1, min(int(count), 12))
        picks = pick_test_frames(spec["shots"], count)
        if not picks:
            raise ValueError("This project has no frames to test.")
        tasks = []
        for shot, frame in picks:
            if tasks and tasks[-1]["shot"] == shot:
                tasks[-1]["frames"].append(frame)
            else:
                tasks.append({"shot": shot, "frames": [frame], "name": TEST_NAME})
        with self.lock:
            if any(j["kind"] == "test" and j["project"] == project and j["state"] in ACTIVE for j in self.jobs):
                raise ValueError("A test render for this project is already queued or running.")
            job = self._new_job("test", spec, "Test render: %d frame%s" % (len(picks), "" if len(picks) == 1 else "s"),
                                tasks, len(picks))
            # Tests go ahead of queued renders so you can check frames before committing to hours.
            idx = next((i for i, j in enumerate(self.jobs) if j["state"] == "queued" and j["kind"] != "test"),
                       len(self.jobs))
            self.jobs.insert(idx, job)
            self._save(force=True)
        return {"created": [job["id"]]}

    # ------------------------------------------------------------ controls
    def cancel(self, job_id):
        with self.lock:
            j = self.find(job_id)
            if not j:
                raise KeyError(job_id)
            if j["state"] == "queued":
                j["state"] = "cancelled"
                j["error"] = None
            elif j["state"] == "running":
                j["cancel_requested"] = True
                p = self.procs.get(job_id)
                if p:
                    threading.Thread(target=self._stop_proc, args=(p,), daemon=True).start()
            self._save(force=True)

    @staticmethod
    def _stop_proc(p):
        try:
            p.terminate()
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
        except OSError:
            pass

    def resume(self, job_id):
        with self.lock:
            j = self.find(job_id)
            if not j:
                raise KeyError(job_id)
            if j["state"] not in RESUMABLE:
                raise ValueError("Only interrupted, cancelled or failed jobs can be resumed.")
            j.update(state="queued", error=None, error_kind=None, returncode=None, finished=None)
            j.pop("cancel_requested", None)
            self._save(force=True)

    def remove(self, job_id):
        with self.lock:
            j = self.find(job_id)
            if not j:
                raise KeyError(job_id)
            if j["state"] == "running":
                raise ValueError("Cancel the job before removing it.")
            self.jobs.remove(j)
            self._save(force=True)

    def clear_finished(self):
        """Remove done jobs from the list (frames stay on disk)."""
        with self.lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j["state"] != "done"]
            self._save(force=True)
            return before - len(self.jobs)

    def shutdown(self):
        """RenderHub is closing: stop renders, mark them interrupted (frames are kept)."""
        with self.lock:
            procs = list(self.procs.items())
            for jid, _ in procs:
                j = self.find(jid)
                if j:
                    j["shutdown"] = True
        for _, p in procs:
            self._stop_proc(p)
        with self.lock:
            for jid, _ in procs:
                j = self.find(jid)
                if j and j["state"] == "running":
                    j["state"] = "interrupted"
                    j["error"] = "RenderHub was closed. Frames on disk are kept; Resume continues."
            self._save(force=True)
        self.sleep.set_active(False)

    # ------------------------------------------------------------ runner
    def start(self):
        threading.Thread(target=self._loop, name="job-runner", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                util.log_exception("job runner")
            time.sleep(0.5)

    def _tick(self):
        to_start = None
        with self.lock:
            running = [j for j in self.jobs if j["state"] == "running"]
            if len(running) < self.cfg.get("max_concurrent_jobs"):
                to_start = next((j for j in self.jobs if j["state"] == "queued"), None)
                if to_start:
                    to_start["state"] = "running"
                    to_start["started"] = util.now_iso()
            busy = any(j["state"] in ACTIVE for j in self.jobs)
        self.sleep.set_active(busy and self.cfg.get("prevent_sleep"))
        if to_start:
            try:
                self._launch(to_start)
            except Exception as e:
                util.log_exception("start job %s" % to_start["id"])
                with self.lock:
                    to_start.update(state="failed", error=str(e) or type(e).__name__,
                                    finished=util.now_iso())
                    self._save(force=True)
        if time.time() - self._last_disk > 3:
            self._last_disk = time.time()
            with self.lock:
                running = [j for j in self.jobs if j["state"] == "running"]
            for j in running:
                self._count_disk(j)
        if self._dirty:
            self._save()

    def _job_frames(self, j):
        """[(shot, frame, path)] for every frame the job must produce."""
        out = []
        base = self.output_dir(j["project"])
        for t in j["tasks"]:
            frames = t["frames"] if "frames" in t else range(t["range"][0], t["range"][1] + 1)
            d = base / ("_test" if j["kind"] == "test" else t["shot"])
            for f in frames:
                out.append((t["shot"], f, d / t["name"].format(frame=f, shot=t["shot"])))
        return out

    def _count_disk(self, j):
        if j["kind"] in ("test", "video"):
            return  # progress comes from events
        n = sum(1 for _, _, p in self._job_frames(j) if frame_exists(p))
        with self.lock:
            j["done"] = n

    def _launch(self, j):
        if j["kind"] == "video":
            return self._launch_video(j)
        spec = self.scanner.render_spec(j["project"])
        try:
            self._check_renderable(spec)
        except ValueError as e:
            raise RuntimeError("Not started: %s" % e)
        sel = self.locator.selected()
        if not sel:
            raise RuntimeError("Blender not found. Set its path in Settings.")
        choice = self.devices.get(j["project"]) if self.devices else None
        hw = self.hw.summary(choice)
        backend = hw["chosen_backend"]
        if backend is None:
            raise RuntimeError("No render device: %s" % hw["chosen_reason"])
        m = spec["manifest"]
        shots = {s["id"]: s for s in spec["shots"]}
        # one GPU per job when several GPUs and several jobs run at once
        device_ids = []
        gpus = [g for g in hw["gpus"] if g.get("id")]
        if choice and choice.get("devices") and backend != "CPU":
            device_ids = [g["id"] for g in gpus]  # exactly the GPUs picked for this project
        elif backend != "CPU" and len(gpus) > 1 and self.cfg.get("max_concurrent_jobs") > 1:
            with self.lock:
                taken = {d for x in self.jobs if x["state"] == "running" and x is not j for d in x.get("device_ids", [])}
            free = [g["id"] for g in gpus if g["id"] not in taken]
            device_ids = [free[0]] if free else [gpus[0]["id"]]
        folder = Path(spec["path"])
        from .package import fix_path, pkg_path
        tasks = []
        base = self.output_dir(j["project"])
        for t in j["tasks"]:
            s = shots.get(t["shot"])
            if s is None:
                raise RuntimeError("Shot %s is no longer in project.json." % t["shot"])
            frames = t["frames"] if "frames" in t else list(range(t["range"][0], t["range"][1] + 1))
            tasks.append({"shot": s["id"], "scene_name": s["scene_name"], "camera": s["camera"],
                          "samples": s["samples"], "fixes": [str(fix_path(folder, f)) for f in s["fixes"]],
                          "frames": frames, "name": t["name"],
                          "out_dir": str(base / ("_test" if j["kind"] == "test" else s["id"]))})
        blend = pkg_path(folder, m["blend_file"])
        args = {"package": str(folder), "blend": str(blend), "render": m["render"], "backend": backend,
                "device_ids": device_ids, "global_fixes": [str(fix_path(folder, f)) for f in m["global_fixes"]],
                "skip_existing": j["kind"] != "test", "parent_pid": os.getpid(), "tasks": tasks,
                "use_cpu": hw["use_cpu"], "threads": hw["cpu_threads"]}
        tmp = self.state_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        args_file = tmp / ("job-%s.json" % j["id"])
        util.write_json_atomic(args_file, args)
        log_path = self.cfg.folder("logs_dir") / ("%s.log" % j["id"])
        cmd = [sel["path"], "-b", str(blend), "--factory-startup", "-noaudio", "--disable-autoexec",
               "--python", str(SCRIPTS_DIR / "render_worker.py"), "--", str(args_file)]
        with self.lock:
            j.update(bench_key=self.settings_key(spec, hw, sel), backend=backend, device_ids=device_ids,
                     devices=[], pid=None, current=None, startup_seconds=None, error=None, error_kind=None, returncode=None,
                     warnings=[], log=str(log_path))
            j["attempts"] += 1
            if j["kind"] == "test":
                j["done"] = 0
                j["test_frames"] = []
        self._spawn(j, cmd, args_file, log_path, hw["chosen_label"])

    def _spawn(self, j, cmd, args_file, log_path, label, cwd=None):
        """Start the Blender process for a job and a thread that reads its output."""
        kwargs = {}
        if util.IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # no window, own process group
        logf = open(log_path, "a", encoding="utf-8")
        logf.write("\n===== %s  attempt %d  %s  %s =====\n%s\n\n" % (util.now_iso(), j["attempts"], j["label"],
                                                                     label, " ".join('"%s"' % c for c in cmd)))
        logf.flush()
        t0 = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd, **kwargs)
        if util.IS_WINDOWS:
            try:
                _win_kill_with_us(p)
            except Exception as e:
                util.log("Could not tie Blender to RenderHub's lifetime: %s" % e)
        with self.lock:
            self.procs[j["id"]] = p
            j["pid"] = p.pid
            j["proc_started"] = t0
            self._save(force=True)
        self._count_disk(j)
        util.log("Job %s started: %s %s (%s)" % (j["id"], j["project"], j["label"], label))
        threading.Thread(target=self._watch, args=(j, p, logf, args_file, t0), name="job-" + j["id"],
                         daemon=True).start()

    def _launch_video(self, j):
        spec = self.scanner.render_spec(j["project"])
        if spec is None:
            raise RuntimeError("Project not found.")
        sel = self.locator.selected()
        if not sel:
            raise RuntimeError("Blender not found. Set its path in Settings.")
        frames, missing = self.project_frames(spec)
        engine, why = self.video_engine(spec)
        m = spec["manifest"]
        if missing and not (engine == "ffmpeg" and (m.get("video") or {}).get("frame_policy")):
            raise RuntimeError("%d frames are missing (first: %s frame %d). Render them, then make the video."
                               % (len(missing), missing[0][0], missing[0][1]))
        out = self.video_path(j["project"])
        out.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.folder("logs_dir") / ("%s.log" % j["id"])
        if engine == "ffmpeg":
            ff = self.ffmpeg_info()
            work = self.state_dir / "tmp" / ("assemble-%s" % j["id"])
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            tmp_out = out.parent / (".partial_" + out.name)
            cmd, info = assemble.build(m, spec["path"], frames, work, tmp_out, ff)
            with self.lock:
                j.update(total=info["out_frames"], done=0, backend=None, devices=["ffmpeg " + str(ff["version"])], pid=None,
                         current=None, startup_seconds=None, error=None, error_kind=None, returncode=None,
                         warnings=info["notes"], log=str(log_path), frame_times=[], bench_key=None, engine="ffmpeg",
                         tmp_out=str(tmp_out), work_dir=str(work))
                j["attempts"] += 1
            util.log("Finished video for %s: %d captions, %.2f s, %dx%d" % (j["project"], info["captions"],
                                                                         info["total_seconds"], *info["size"]))
            self._spawn(j, cmd, None, log_path, "ffmpeg", cwd=str(work))
            return
        r = m["render"]
        args = {"frames": [str(f[2]) for f in frames], "fps": r["fps"], "width": r["resolution_x"],
                "height": r["resolution_y"], "out": str(out), "parent_pid": os.getpid()}
        tmp = self.state_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        args_file = tmp / ("job-%s.json" % j["id"])
        util.write_json_atomic(args_file, args)
        cmd = [sel["path"], "-b", "--factory-startup", "-noaudio", "--disable-autoexec",
               "--python", str(SCRIPTS_DIR / "make_video.py"), "--", str(args_file)]
        with self.lock:
            j.update(total=len(frames), done=0, backend=None, devices=[], pid=None, current=None,
                     startup_seconds=None, error=None, error_kind=None, returncode=None,
                     warnings=[why] if why else [], log=str(log_path), frame_times=[], bench_key=None, engine="blender")
            j["attempts"] += 1
        self._spawn(j, cmd, args_file, log_path, "video")

    def _watch(self, j, p, logf, args_file, t0):
        tail = []
        events = {"done": False, "error": None, "kind": None}
        is_ffmpeg = j.get("engine") == "ffmpeg" and j["kind"] == "video"
        try:
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if is_ffmpeg and PROGRESS_RE.match(line):  # ffmpeg -progress lines: progress, not log
                    k, _, val = line.partition("=")
                    if k == "frame" and val.strip().isdigit():
                        with self.lock:
                            j["done"] = min(j["total"], int(val))
                    elif k == "progress" and val.strip() == "end":
                        events["done"] = True
                    continue
                logf.write(line + "\n")
                logf.flush()
                tail.append(line)
                del tail[:-200]
                if line.startswith(EV):
                    try:
                        ev = json.loads(line[len(EV):])
                    except ValueError:
                        continue
                    self._on_event(j, ev, events, t0)
        except Exception:
            util.log_exception("reading job %s" % j["id"])
        rc = p.wait()
        logf.write("\n===== exit code %s after %.1fs =====\n" % (rc, time.time() - t0))
        logf.close()
        if args_file:
            try:
                args_file.unlink()
            except OSError:
                pass
        if is_ffmpeg:
            tmp_out = j.get("tmp_out")
            try:
                if rc == 0 and tmp_out and os.path.getsize(tmp_out) > 0:
                    os.replace(tmp_out, str(self.video_path(j["project"])))  # complete: move into place
                    shutil.rmtree(j.get("work_dir") or "", ignore_errors=True)
                elif tmp_out and os.path.exists(tmp_out):
                    os.remove(tmp_out)  # never leave a broken video behind
            except OSError as e:
                util.log("Finishing the video of %s failed: %s" % (j["project"], e))
                rc = rc or 1
        self._finish(j, rc, events, tail, t0)

    def _on_event(self, j, ev, events, t0):
        e = ev.get("event")
        with self.lock:
            if e == "start":
                j["devices"] = ev.get("devices", [])
            elif e == "warning":
                j["warnings"].append(ev.get("message"))
            elif e == "frame_start":
                if j.get("startup_seconds") is None:
                    j["startup_seconds"] = round(time.time() - t0, 1)
                j["current"] = {"shot": ev["shot"], "frame": ev["frame"], "since": time.time()}
            elif e == "frame_skip":
                pass
            elif e == "frame_done":
                sec = float(ev.get("seconds") or 0)
                j["frame_times"].append([ev["shot"], ev["frame"], sec])
                del j["frame_times"][:-50]
                j["current"] = None
                if j["kind"] == "video":
                    j["done"] = min(j["total"], j["done"] + 1)
                elif j["kind"] == "test":
                    j["done"] += 1
                    rel = os.path.relpath(ev["path"], str(self.cfg.folder("output_dir"))).replace("\\", "/")
                    j["test_frames"].append({"shot": ev["shot"], "frame": ev["frame"], "seconds": sec, "image": rel})
                else:
                    j["done"] = min(j["total"], j["done"] + 1)
                    self.bench.add_measured(j["bench_key"], j["project"], ev["shot"], sec)
            elif e == "error":
                events["error"] = ev.get("message")
                events["kind"] = ev.get("kind")
            elif e == "done":
                events["done"] = True
        self._dirty = True

    def _finish(self, j, rc, events, tail, t0):
        text = "\n".join(tail)
        with self.lock:
            self.procs.pop(j["id"], None)
            j["returncode"] = rc
            j["pid"] = None
            j["current"] = None
            j["run_seconds"] = round(j.get("run_seconds", 0) + time.time() - t0, 1)
            j["finished"] = util.now_iso()
        self._count_disk(j)
        with self.lock:
            missing = [f for _, f, pth in self._job_frames(j) if not frame_exists(pth)] if j["kind"] == "render" else []
            if j.pop("shutdown", False):
                j["state"] = "interrupted"
                j["error"] = "RenderHub was closed. Frames on disk are kept; Resume continues."
            elif j.pop("cancel_requested", False):
                j["state"] = "cancelled"
                j["error"] = "Cancelled. %d of %d frames are on disk; Resume continues from there." % (j["done"], j["total"])
            elif rc == 0 and events["done"] and not missing and (j["kind"] != "video" or self.video_path(j["project"]).exists()):
                j["state"] = "done"
                j["error"] = None
                if j["kind"] == "video":
                    j["done"] = j["total"]
            else:
                j["state"] = "failed"
                if OOM_RE.search(text) or OOM_RE.search(events["error"] or ""):
                    j["error_kind"] = "out_of_memory"
                    j["error"] = ("Out of memory (GPU or system). Lower the number of concurrent jobs to 1, close other "
                                  "apps, or lower samples/resolution in the scene. Frames so far are kept.")
                elif events["error"]:
                    j["error_kind"] = events["kind"]
                    j["error"] = events["error"].strip().splitlines()[-1] if events["kind"] == "exception" else events["error"]
                    if events["kind"] == "exception":
                        j["error"] = "Blender script error: " + j["error"]
                elif rc == 0 and missing:
                    j["error"] = "Blender finished but %d frames are missing (first: %d)." % (len(missing), missing[0])
                else:
                    j["error"] = "%s stopped unexpectedly (exit code %s). Last lines:\n%s" % (
                        "ffmpeg" if j.get("engine") == "ffmpeg" and j["kind"] == "video" else "Blender", rc, util.tail(text, 6))
            if j["startup_seconds"] is not None and j["kind"] == "render":
                self.bench.add_startup(j["bench_key"], j["project"], j["startup_seconds"])
            state = j["state"]
            self._save(force=True)
        if j["kind"] == "test" and j.get("test_frames"):
            self.bench.set_test(j["bench_key"], j["project"], {
                "at": j["finished"], "job": j["id"], "frames": j["test_frames"], "backend": j["backend"],
                "devices": j["devices"], "startup_seconds": j["startup_seconds"], "complete": state == "done"})
        else:
            self.bench.save()
        util.log("Job %s %s%s" % (j["id"], state, (": " + j["error"].splitlines()[0]) if j["error"] else ""))
        if state == "done" and j["kind"] == "render":
            self._maybe_auto_video(j["project"])

    def _maybe_auto_video(self, project):
        """When the last shot of a project is done, queue the video automatically."""
        with self.lock:
            if any(x["project"] == project and x["kind"] in ("render", "video") and x["state"] in ACTIVE for x in self.jobs):
                return
        try:
            self.create_video_job(project, auto=True)
        except ValueError as e:
            util.log("No automatic video for %s: %s" % (project, e))

    # ------------------------------------------------------------ views and estimates
    def _job_eta(self, j, now):
        """(seconds remaining or None, seconds per frame or None, source)"""
        remaining = max(0, j["total"] - j["done"])
        if j["kind"] == "video":
            if j["state"] == "running" and j["done"] and j.get("proc_started"):
                spf = (now - j["proc_started"]) / j["done"]
                return remaining * spf, spf, "live"
            return None, None, None
        recent = [ft[2] for ft in j["frame_times"][-5:]]
        if recent:
            spf, src = sum(recent) / len(recent), "live"
        else:
            shot = j["tasks"][0]["shot"]
            spf, src = self.bench.spf(j["bench_key"] or self._key_for(j["project"]), shot)
            if spf is None:
                vals = self.bench.all_spf(j["bench_key"] or self._key_for(j["project"]))
                spf, src = (sum(vals) / len(vals), "from other shots") if vals else (None, None)
        if spf is None:
            return None, None, None
        eta = remaining * spf
        cur = j.get("current")
        if j["state"] == "running" and cur:
            eta -= min(now - cur["since"], spf * 0.95)
        elif j["state"] == "running" and not cur and not j["frame_times"]:
            eta += max(0, (self.bench.startup(j["bench_key"]) or DEFAULT_STARTUP) - (now - j.get("proc_started", now)))
        elif j["state"] == "queued":
            eta += self.bench.startup(self._key_for(j["project"]) or "") or DEFAULT_STARTUP
        return max(0.0, eta), spf, src

    def _key_for(self, project):
        spec = self.scanner.render_spec(project)
        if not spec:
            return None
        return self.settings_key(spec)

    def view(self):
        now = time.time()
        with self.lock:
            jobs = [dict(j) for j in self.jobs]
        out = []
        total_eta, unknown = 0.0, 0
        for j in jobs:
            row = {k: j.get(k) for k in ("id", "kind", "project", "title", "label", "state", "total", "done", "error",
                                         "error_kind", "started", "finished", "backend", "devices", "warnings",
                                         "attempts", "test_frames")}
            run = j.get("run_seconds", 0) + ((now - j["proc_started"]) if j["state"] == "running" and j.get("proc_started") else 0)
            row["elapsed"] = fmt_duration(run) if run else None
            cur = j.get("current")
            row["current"] = ("%s frame %d (%ds so far)" % (cur["shot"], cur["frame"], now - cur["since"])) if cur else (
                "starting Blender..." if j["state"] == "running" else None)
            if j["state"] in ACTIVE:
                eta, spf, src = self._job_eta(j, now)
                row["eta_seconds"] = eta
                row["eta"] = fmt_duration(eta) if eta is not None else ("estimating..." if j["kind"] == "video" else "unknown, run a test render")
                row["spf"] = round(spf, 2) if spf else None
                row["fpm"] = round(60.0 / spf, 1) if spf else None
                row["eta_source"] = src
                if eta is None:
                    unknown += 1
                else:
                    total_eta += eta
            out.append(row)
        conc = max(1, self.cfg.get("max_concurrent_jobs"))
        active = [r for r in out if r["state"] in ACTIVE]
        return {"jobs": out, "active": len(active),
                "total_eta": fmt_duration(total_eta / min(conc, max(1, len(active)))) if active and total_eta else None,
                "unknown_eta_jobs": unknown, "concurrency": conc, "keep_awake": self.sleep.active}

    def log_tail(self, job_id, lines=60):
        j = self.find(job_id)
        if not j:
            raise KeyError(job_id)
        p = j.get("log")
        if not p or not os.path.exists(p):
            return ""
        with open(p, "rb") as f:
            f.seek(max(0, os.path.getsize(p) - 64 * 1024))
            text = f.read().decode("utf-8", errors="replace")
        return util.tail(text, lines)

    def project_extras(self, project):
        """For the project page: frames already on disk per shot, the latest test, the estimate."""
        spec = self.scanner.render_spec(project)
        if not spec or not spec["shots"]:
            return {}
        key = self.settings_key(spec)
        base = self.output_dir(project)
        on_disk = {s["id"]: 0 for s in spec["shots"]}
        newest = 0
        frames, missing = self.project_frames(spec)
        for shot, _, path in frames:
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > 0:
                on_disk[shot] += 1
                newest = max(newest, st.st_mtime)
        test_key, test = self.bench.latest_test(project)
        if test:
            test = dict(test, stale=(test_key != key))
        return {"on_disk": on_disk, "test": test, "estimate": self.estimate(spec, key, on_disk),
                "output_dir": str(base), "frames_complete": bool(frames) and not missing,
                "video": self.video_info(project, newest),
                "video_job": next((j["state"] for j in self.jobs if j["kind"] == "video" and j["project"] == project
                                   and j["state"] in ACTIVE), None),
                "devices": self.device_panel(project),
                "video_engine": dict(zip(("engine", "why"), self.video_engine(spec)))}

    def device_panel(self, project):
        """What the project's Render with box shows: the choice, the options on this PC, the result."""
        base = self.hw.summary()
        choice = (self.devices.get(project) if self.devices else None) or {"backend": "auto", "devices": [], "use_cpu": False,
                                                                            "threads": 0}
        res = self.hw.summary(choice)
        auto = self.hw.summary({"backend": "auto"}) if choice["backend"] != "auto" else res
        from .hardware import BACKEND_LABEL
        return {"choice": choice,
                "backends": [{"type": b["type"], "label": BACKEND_LABEL.get(b["type"], b["type"]), "devices": b["devices"],
                              "cpu_hybrid": b.get("cpu_hybrid", False)} for b in base["backends"] if b.get("usable")],
                "cpu": {"name": (base["cpu_devices"] or [base["cpu"]])[0], "threads": base["threads"]},
                "auto_label": "%s: %s" % (auto["chosen_label"], auto["devices_text"]) if auto["chosen_backend"] else "none",
                "resolved": {"ok": res["chosen_backend"] is not None, "label": res["chosen_label"], "text": res["devices_text"],
                             "reason": res["chosen_reason"], "vram_gb": res["vram_gb"]},
                "detecting": base["detecting"]}

    def video_info(self, project, newest_frame=None):
        v = self.video_path(project)
        try:
            st = os.stat(v)
        except OSError:
            return None
        rel = os.path.relpath(str(v), str(self.cfg.folder("output_dir"))).replace("\\", "/")
        return {"url": "/files/output/" + rel, "path": str(v), "size": st.st_size,
                "made": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "outdated": bool(newest_frame and newest_frame > st.st_mtime + 1)}

    def forget_project(self, project):
        """Drop a removed project's jobs (none may be active)."""
        with self.lock:
            if any(j["project"] == project and j["state"] in ACTIVE for j in self.jobs):
                raise ValueError("This project has queued or running jobs. Cancel them first.")
            self.jobs = [j for j in self.jobs if j["project"] != project]
            self._save(force=True)

    def estimate(self, spec, key, on_disk):
        shots = [s for s in spec["shots"] if s["render_frames"]]
        known = self.bench.all_spf(key)
        r = self.bench.get(key) or {}
        n_test = len(((r.get("test") or {}).get("frames")) or [])
        n_meas = sum(m[1] for m in (r.get("measured") or {}).values())
        if not known:
            _, latest = self.bench.latest_test(spec["id"])
            why = ("The last test render used other settings, another device or an older copy of the package; run it again."
                   if latest else "Run a test render.")
            return {"known": False, "text": "unknown. " + why}
        lo = hi = 0.0
        remaining_total = 0
        per_shot = []
        for s in shots:
            remaining = s["render_frames"] - on_disk.get(s["id"], 0)
            remaining_total += remaining
            spf, src = self.bench.spf(key, s["id"])
            if spf is None:
                a, b = min(known), max(known)
                src = "other shots"
            else:
                a, b = spf * 0.85, spf * 1.2
            lo += remaining * a
            hi += remaining * b
            per_shot.append({"shot": s["id"], "spf": round(spf, 2) if spf else None, "source": src,
                             "remaining": remaining})
        n_jobs = sum(1 for s in shots if s["render_frames"] - on_disk.get(s["id"], 0) > 0)
        startup = (self.bench.startup(key) or (r.get("test") or {}).get("startup_seconds") or DEFAULT_STARTUP)
        lo += n_jobs * startup * 0.5
        hi += n_jobs * startup
        conc = max(1, min(self.cfg.get("max_concurrent_jobs"), max(1, len(shots))))
        lo, hi = lo / conc, hi / conc
        basis = []
        if n_test:
            basis.append("%d test frame%s" % (n_test, "" if n_test == 1 else "s"))
        if n_meas:
            basis.append("%d rendered frame%s" % (n_meas, "" if n_meas == 1 else "s"))
        if remaining_total == 0:
            text = "all frames are already on disk."
        else:
            text = "%s to %s for %d remaining frames (based on %s)." % (fmt_duration(lo), fmt_duration(hi),
                                                                       remaining_total, " and ".join(basis))
        return {"known": True, "low_seconds": lo, "high_seconds": hi, "text": text, "per_shot": per_shot,
                "remaining_frames": remaining_total}
