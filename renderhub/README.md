# RenderHub

Renders Blender "project packages" on this PC with whatever hardware it has. Localhost only
(127.0.0.1), Python standard library only, nothing to install except Python 3.10+ and Blender.

Status: **milestone 2** (detection, requirements, test render, rendering with progress, ETA and
resume). Pause/reorder, out-of-memory retry, outputs/MP4 and the exporter come in milestones 3-4.

## Start

From a command prompt (Windows: Command Prompt or PowerShell; Mac: Terminal):

```
cd C:\studio\renderhub          (Mac: cd /path/to/studio/renderhub)
py run.py                        (Mac/Linux: python3 run.py)
```

The page opens in your browser at <http://127.0.0.1:8800/>. Keep the command window open:
closing it stops RenderHub (running renders become "interrupted" and can be resumed).
Double-clicking `start.bat` (Windows) or `start.command` (Mac) does the same.
Options: `--port 8801`, `--no-browser`.

## The everyday flow

1. **Upload**: in *2. Projects*, drop the project .zip on the dashed box (or Choose File). A progress
   bar shows the upload; RenderHub then extracts and checks it, and selects the new project.
2. **Check**: the project shows Ready, Warnings, Blocked or Invalid with a checklist. Optional but
   recommended: **Test render** to see a few frames and get a time estimate.
3. **Start rendering**: the button on the project's row (or in its detail) queues every shot.
   Progress, ETA and logs are in *4. Queue*; frames land in `output/<project>/<shot>/`.

Uploads are saved into `projects/`, extracted to `projects/<name>/`, and RenderHub's copy of the zip
is then removed (your original file is untouched). If a project of the same name is already there,
the zip is not extracted: rename or remove the old folder first, so nothing is ever overwritten.
Copying a folder or zip straight into `projects/` still works too.

## Render

1. **Test render** first. It renders a few frames spread across the project (by default the first
   frame of the first shot, the middle of a middle shot, the last frame of the last shot) at the real
   settings, shows the images, and measures seconds per frame. **Look at the images**: some problems
   (denoiser blotches, magenta textures, missing characters) only show on one GPU backend or late in a shot.
2. The project page then shows an **estimate** as a range ("based on N test frames"). Without a test
   it says "unknown" rather than guessing. Real renders keep refining it.
3. **Render selected** (tick shots) or **Render all**. One job per shot; frames go to
   `output/<project>/<shot>/frame_00073.png` (the frame number from the .blend).
4. The **Queue** shows progress (frames on disk / frames in the job), the frame being rendered,
   elapsed time, ETA, frames per minute, and the log.

Only Ready and Warnings projects can render. Jobs run one at a time by default (Settings: max
concurrent jobs). While jobs run the PC is kept awake (Settings: Keep awake).

**Stopping and resuming.** Cancel stops the job and keeps its frames. If RenderHub is closed or
crashes, running jobs show as *interrupted* after the restart. **Resume** (or **Retry** for a failed
job) continues: frames already on disk are skipped, never rendered twice. Each frame is written to a
hidden temp file and renamed when complete, so a killed render never leaves a half-written frame.
Blender stops by itself when RenderHub goes away (on Windows it is tied to RenderHub's lifetime; on
macOS/Linux it finishes the current frame and exits). To re-render a frame, delete its file first.

**Disk full** stops the job with a clear message before the drive is completely full.
**Out of memory** is recognised in Blender's output and reported as such.

## Folders

```
studio/
  renderhub/   this program (config.json holds the settings)
  projects/    drop packages here (never modified or deleted by RenderHub)
  output/      <project>/<shot>/frame_NNNNN.png, <project>/_test/ test frames, <project>/final/ the MP4
  trash/       removed projects (delete it yourself to free space)
  logs/        renderhub.log, <job-id>.log per job, check-<project>.log, probe_devices.log
  state/       jobs.json (the queue), benchmarks.json (seconds per frame), and caches:
               hardware.json, checks.json, hashes.json, imports.json. Safe to delete while
               RenderHub is closed: caches are rebuilt, but the queue and timings are lost.
```

Keep `projects/` and `output/` on a fast SSD. The checklist warns when either is on a hard disk
or a nearly full drive.

## Package format (project.json, schema 1)

```json
{
  "schema_version": 1,
  "name": "frozen-100-years",
  "title": "Frozen 100 Years",
  "blend_file": "scene.blend",
  "blender": {"min_version": "4.2", "built_with": "4.2.3"},
  "render": {"fps": 30, "resolution_x": 1080, "resolution_y": 1920, "format": "PNG",
             "color_depth": "8", "samples": 128, "denoise": true, "persistent_data": true},
  "requirements": {"min_vram_gb": 8, "min_ram_gb": 16, "recommended_vram_gb": 12,
                   "est_disk_gb": 6, "gpu_required": false},
  "shots": [
    {"id": "S01", "frame_start": 0, "frame_end": 72},
    {"id": "S03", "frame_start": 240, "frame_end": 507, "fixes": ["S03_denoise_off.py"],
     "notes": "OIDN blob workaround"}
  ],
  "global_fixes": [],
  "integrity": {"algorithm": "sha256", "image_count": 12, "files": {"scene.blend": {"size": 1, "sha256": "..."}}},
  "post": [],
  "created": {"date": "2026-11-23", "exporter": "1"}
}
```

Frame ranges are inclusive and count in the shot's own scene (`scene_name`). Shots in different
scenes never affect each other, so the usual layout (one scene per shot, each numbered from 1) renders
every frame. Within one scene (or among shots with no `scene_name`), if two shots share a frame
(S01 ends at 72, S02 starts at 72), the frame is rendered once, by the earlier shot; the checklist
says so. The video plays shots in the order they are listed. `render.resolution_x/y` is the final
size (the worker renders at 100%), so a scene meant for 80% must declare the smaller size. Fix scripts live in `fixes/`
and may be listed by file name.

## Settings (page section 6, stored in config.json)

| Setting | What it does |
|---|---|
| Blender path | Blender to use. Empty = search config, PATH, then standard install folders; the newest found is used. Accepts `Blender.app`, a folder, or the program file. |
| Device | `auto` picks OptiX, CUDA, HIP, Metal, oneAPI, then CPU. Choose one to force it, or `CPU`. |
| VRAM override | GPU memory in GB, for GPUs whose VRAM cannot be read (AMD on Windows is often unreadable; an RX 7900 XT has 20). |
| Max concurrent jobs | Blender renders at once. Default 1: two jobs on one GPU ran out of memory before. The page warns above 1 per GPU. |
| Chunk size | Split shots longer than this many frames into several jobs. 0 = one job per shot. |
| Port | Page port. Restart to apply. |
| ffmpeg path | For making the MP4 (milestone 4). Empty = PATH. |
| Keep awake | Stop the PC sleeping while jobs run (Windows power request, macOS `caffeinate`, Linux `systemd-inhibit`). |

Advanced (config.json only): `projects_dir`, `output_dir`, `logs_dir`, `state_dir` (relative to the
studio folder or absolute), `settle_seconds`, `scan_interval_seconds`, `max_zip_extract_gb`.

## How detection works

- **Devices** come from Blender itself (`blender_scripts/probe_devices.py`, run headless with
  factory settings). Cached in `state/hardware.json`; press **Re-detect** after a driver change.
- **VRAM**: Settings override, then `nvidia-smi` (NVIDIA), unified memory (Apple Silicon), the
  Windows registry (AMD/Intel, best effort), Linux sysfs (AMD). Otherwise "unknown", and the VRAM
  check becomes a warning instead of a failure.
- **Missing files**: `blender_scripts/check_project.py` opens the .blend headless (auto-run scripts
  off) and lists missing textures, linked libraries, fonts, caches, broken fix scripts, and shot
  scenes/cameras that do not exist. Results are cached until the package or Blender changes;
  **Check again** forces a fresh check, including checksums.

## Choosing devices per project

Each project page has a **Render with** box, set before you press Start:

- **Render on**: *Automatic* (the best backend on this PC, or the Settings default), one GPU backend
  (OptiX, CUDA, HIP, Metal, oneAPI: whatever this PC has), or *CPU only*.
- **GPUs**: tick which GPUs of that backend the project may use. With several jobs at once each job
  takes one ticked GPU.
- **CPU**: also use the CPU together with the GPU, where Cycles allows it. Not always faster (on an
  Apple M5 it was about half the speed of the GPU alone), so compare with a test render.
- **CPU threads**: 0 uses all; fewer keeps the PC usable while it renders.

The choice is saved per project in `state/project_devices.json` on this PC, never in the package,
and is read when each job starts. The checklist and estimate follow it: a backend this PC lacks
shows as Blocked with the reason, and a new choice needs a new test render for its estimate.
The project list shows what each project renders with.

## The video

When the last shot of a project finishes, RenderHub makes the video automatically into
`output/<project>/` (at `video.output` from project.json, else `final/<project>.mp4`). Press
**Play video** on the project's row to watch it in the page; **Open in video player**, **Open folder**,
**Download** and **Make again** sit next to it.

- **Finished video (sound, captions, text)** when project.json has a `video` section and ffmpeg is
  found: frames in shot order, prescale and grain, dips to black, on-screen texts, captions from the
  captions file, voice + sfx mixed and loudness-normalised, encoded as set in `video.encode`. It follows
  the recipe the existing videos were made with. ffmpeg must have drawtext (FreeType); the checklist
  item "Finished video" says whether it is ready and what goes in.
- **Silent video** otherwise (no `video` section, ffmpeg missing, or its inputs are incomplete), joined
  by Blender itself; the page says why.

ffmpeg on Windows: `winget install Gyan.FFmpeg`, or download a full build and put the path to
`ffmpeg.exe` in Settings. RenderHub also looks on PATH and in the usual install folders.

## Removing a project

**Remove** (on the project's row or page) moves the project folder, and the zip it came from, to
`trash/<date>-<name>/` next to `projects/`. It also asks whether to move the rendered frames and
video there too. Nothing is deleted: move a folder back from `trash/` to restore it, or delete
`trash/` yourself to free the space. A project with queued or running jobs cannot be removed until
they are cancelled.

## Fix scripts

Scripts in `fixes/` run inside Blender after the manifest settings are applied, global fixes first,
then the shot's own, so a fix can override samples or denoising for one shot. They get `bpy`,
`scene` (the shot's scene) and `shot` (its id). The .blend is reloaded before each shot, so a fix
never leaks into another shot. Example (`fixes/S03_denoise_off.py`):

```python
scene.cycles.use_denoising = False
scene.cycles.samples = 64
for ob in scene.objects:
    if ob.type == "LIGHT":
        ob.visible_camera = False
```

Auto-run Python scripts stay off. If a scene has drivers with Python expressions, the job shows a
warning, because those drivers will not update; check the test frames.

## Troubleshooting

| Message | What to do |
|---|---|
| Port 8800 is already in use | RenderHub is probably already open: use that window. Or run `python run.py --port 8801`. |
| RenderHub is already running on this PC | Another RenderHub window is open. Close it first. |
| Blender was not found | Paste the path in Settings (Windows: `C:\Program Files\Blender Foundation\Blender 4.2\blender.exe`). |
| VRAM unknown | Enter it in Settings (VRAM override). |
| The zip is damaged or incomplete | The copy did not finish. Copy the zip again. |
| A folder projects/X already exists | RenderHub never overwrites. Rename or remove one, and it extracts on the next scan. |
| Checksum mismatch | A file changed or was cut short in the copy. Copy the package again. |
| N missing image/texture files | The package was not exported with its textures. Re-export so they sit in `assets/` with relative paths. |
| Job failed: Out of memory | Keep max concurrent jobs at 1, close other apps, or lower samples/resolution. Retry continues where it stopped. |
| Job interrupted | RenderHub was closed or crashed during the render. Press Resume. |
| Estimate unknown | Run a test render. If settings, device or the package changed since, run it again. |
| Upload failed: Not enough disk space | The zip plus its extracted copy must fit on the projects drive. |
| Python says it needs 3.10 | Install Python from python.org (tick "Add to PATH" on Windows). |

Everything RenderHub does is in `logs/renderhub.log`; each Blender check has its own log.

## Tests

```
python -m unittest discover -s tests          # fast checks, no Blender
python tools/make_test_packages.py            # builds test_packages/ (needs Blender)
```

`make_test_packages.py` writes a tiny good package plus one package for each failure case
(0-byte .blend, no project.json, checksum mismatch, missing texture, broken fix script,
needs Blender 99, truncated zip, zip with a `../` path). Copy them into `projects/` to see each status.
