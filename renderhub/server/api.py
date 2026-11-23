"""Local JSON API and the single page. Binds 127.0.0.1 only and rejects requests whose
Host is not localhost (DNS-rebinding guard). POSTs must be JSON and same-origin."""

import json
import os
import re
import shutil
import socket
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, util
from .config import HELP, RENDERHUB_DIR, STUDIO_DIR, ConfigError

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
WEB_DIR = RENDERHUB_DIR / "web"


class App:
    """Holds the long-lived objects the handlers use."""

    def __init__(self, cfg, locator, hw, scanner, jobs=None):
        self.cfg, self.locator, self.hw, self.scanner, self.jobs = cfg, locator, hw, scanner, jobs


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind the same port; we want a clear failure.
    allow_reuse_address = not util.IS_WINDOWS

    def server_bind(self):
        if util.IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app, port):
    handler = type("BoundHandler", (Handler,), {"app": app, "port": port})
    return Server(("127.0.0.1", port), handler)


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    app = None
    port = None
    server_version = "RenderHub/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ------------------------------------------------------------ plumbing
    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.split(":")[0]
        return name in ALLOWED_HOSTS

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        try:
            return urllib.parse.urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")
        except ValueError:
            return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            raise HttpError(413, "request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HttpError(400, "body is not JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    def _dispatch(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "RenderHub only answers on localhost."})
        path = urllib.parse.urlparse(self.path).path
        try:
            if method == "POST" and path == "/api/upload":
                # Raw file body. The custom header cannot be sent cross-site without a CORS
                # preflight, which RenderHub never approves.
                if self.headers.get("X-RenderHub-Upload") != "1" or not self._origin_ok():
                    self.close_connection = True
                    raise HttpError(403, "Uploads must come from the RenderHub page.")
            elif method == "POST":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json" or not self._origin_ok():
                    raise HttpError(403, "POST must be JSON from the RenderHub page.")
            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                mm = re.fullmatch(pattern, path)
                if mm:
                    args = [urllib.parse.unquote(g) for g in mm.groups()]
                    result = fn(self, *args)
                    if result is not None:
                        self._send(200, result)
                    return
            raise HttpError(404, "not found: %s" % path)
        except HttpError as e:
            self._send(e.code, {"error": e.msg})
        except (ConfigError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except KeyError as e:
            self._send(404, {"error": "not found: %s" % e})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            util.log_exception("%s %s" % (method, path))
            try:
                self._send(500, {"error": "Internal error (%s). Details are in logs/renderhub.log." % type(e).__name__})
            except Exception:
                pass

    def do_HEAD(self):
        self._send(405, {"error": "use GET"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ------------------------------------------------------------ pages
    def page_index(self):
        html = (WEB_DIR / "index.html").read_bytes()
        self._send(200, html, "text/html; charset=utf-8")

    # ------------------------------------------------------------ API
    def api_overview(self):
        a = self.app
        return {"version": VERSION, "studio": str(STUDIO_DIR), "hardware": a.hw.summary(),
                "blender": a.locator.info(), "projects": self._with_video(a.scanner.list()),
                "projects_dir": str(a.cfg.folder("projects_dir")), "output_dir": str(a.cfg.folder("output_dir")),
                "last_scan": a.scanner.last_scan, "scan_error": a.scanner.scan_error,
                "settings": a.cfg.snapshot(), "warnings": settings_warnings(a), "queue": a.jobs.view(),
                "ffmpeg": a.jobs.ffmpeg_info()}

    def _with_video(self, rows):
        for r in rows:
            r["has_video"] = r["kind"] == "folder" and self.app.jobs.video_path(r["id"]).exists()
        return rows

    def api_hardware(self):
        return self.app.hw.summary()

    def api_hardware_refresh(self):
        self._body()
        self.app.hw.redetect()
        return {"ok": True}

    def api_settings(self):
        return {"settings": self.app.cfg.snapshot(), "help": HELP, "warnings": settings_warnings(self.app),
                "blender": self.app.locator.info()}

    def api_settings_save(self):
        a = self.app
        changes = self._body()
        changed = a.cfg.update(changes)
        if "blender_path" in changed:
            a.locator.refresh()
            a.hw.redetect()
            a.scanner.blender_changed()
        if {"projects_dir", "output_dir"} & set(changed):
            a.scanner.rescan()
        restart = "port" in changed or "projects_dir" in changed
        return {"ok": True, "changed": changed, "settings": a.cfg.snapshot(),
                "warnings": settings_warnings(a),
                "notice": "Restart RenderHub for the new port to apply." if "port" in changed else None,
                "restart_needed": restart}

    def api_blender_refresh(self):
        self._body()
        info = self.app.locator.refresh()
        self.app.hw.redetect()
        self.app.scanner.blender_changed()
        return info

    def api_projects(self):
        return self.app.scanner.list()

    def api_rescan(self):
        self._body()
        self.app.scanner.rescan()
        return {"ok": True}

    def api_project(self, name):
        d = self.app.scanner.detail(name)
        if d is None:
            raise HttpError(404, "no project named %s" % name)
        d.update(self.app.jobs.project_extras(name))
        return d

    def api_recheck(self, name):
        self._body()
        if self.app.scanner.detail(name) is None:
            raise HttpError(404, "no project named %s" % name)
        self.app.scanner.recheck(name)
        return {"ok": True}

    def api_test_render(self, name):
        body = self._body()
        return self.app.jobs.create_test_job(name, body.get("frames", 3))

    def api_jobs(self):
        return self.app.jobs.view()

    def api_jobs_create(self):
        body = self._body()
        project = body.get("project")
        if not isinstance(project, str) or not project:
            raise HttpError(400, "project is required")
        shots = body.get("shots")
        if shots is not None and not (isinstance(shots, list) and all(isinstance(x, str) for x in shots)):
            raise HttpError(400, "shots must be a list of shot ids, or omitted for all shots")
        return self.app.jobs.create_render_jobs(project, shots or None)

    def api_job_action(self, job_id, action):
        self._body()
        getattr(self.app.jobs, action)(job_id)
        return {"ok": True}

    def api_upload(self):
        """Stream a project zip into projects/ (never held in memory), then let the scanner import it."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = os.path.basename((q.get("name") or [""])[0].replace("\\", "/")).strip()
        self.close_connection = True  # on any early error the unread body must not be parsed as a request
        if not raw.lower().endswith(".zip"):
            raise HttpError(400, "Only .zip files can be uploaded (a zip of the project folder).")
        stem = re.sub(r"[^\w .()-]", "_", raw[:-4]).strip(" .") or "project"
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length <= 0:
            raise HttpError(411, "The upload has no size; try again.")
        cap = self.app.cfg.get("max_zip_extract_gb") * 1024 ** 3
        if length > cap:
            raise HttpError(413, "The zip is %s, above the %s limit (max_zip_extract_gb in config.json)."
                            % (util.human_bytes(length), util.human_bytes(cap)))
        pdir = self.app.cfg.folder("projects_dir")
        free = shutil.disk_usage(str(pdir)).free
        need = int(length * 2.2) + 1024 ** 3  # the zip, its extracted copy, and a margin
        if free < need:
            raise HttpError(507, "Not enough disk space: the zip and its extracted copy need about %s, %s is free on %s."
                            % (util.human_bytes(need), util.human_bytes(free), pdir))
        tmp = pdir / (".upload-%s.part" % uuid.uuid4().hex[:12])
        got = 0
        try:
            with open(util.longpath(tmp), "wb") as f:
                while got < length:
                    chunk = self.rfile.read(min(8 * 1024 * 1024, length - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if got != length:
                raise HttpError(400, "The upload stopped after %s of %s. Try again."
                                % (util.human_bytes(got), util.human_bytes(length)))
            final, n = pdir / (stem + ".zip"), 2
            while final.exists():
                final = pdir / ("%s-%d.zip" % (stem, n))
                n += 1
            os.replace(util.longpath(tmp), util.longpath(final))
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        util.log("Uploaded %s (%s)" % (final.name, util.human_bytes(length)))
        self.app.scanner.register_upload(final.name)
        return {"ok": True, "zip": final.name, "size": length}

    def api_jobs_clear(self):
        self._body()
        return {"removed": self.app.jobs.clear_finished()}

    def api_job_log(self, job_id):
        return {"log": self.app.jobs.log_tail(job_id, 80)}

    def file_output(self, rel):
        """Serve frames and videos from the output folder (never outside it). Videos support
        byte ranges, which browsers need for seeking (Safari needs them to play at all)."""
        root = os.path.realpath(str(self.app.cfg.folder("output_dir")))
        full = os.path.realpath(os.path.join(root, *[p for p in rel.split("/") if p not in ("", ".", "..")]))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise HttpError(404, "no such file")
        ext = os.path.splitext(full)[1].lower()
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4"}
        if ext not in types:
            raise HttpError(403, "only images and videos are served")
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", types[ext])
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("download"):
            self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % os.path.basename(full).replace('"', ""))
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def api_devices(self, name):
        """Save which devices render this project on this PC."""
        body = self._body()
        if self.app.scanner.entry(name) is None:
            raise HttpError(404, "no project named %s" % name)
        choice = self.app.jobs.devices.set(name, body)
        return {"ok": True, "choice": choice, "devices": self.app.jobs.device_panel(name)}

    def api_make_video(self, name):
        self._body()
        return self.app.jobs.create_video_job(name)

    def api_open_folder(self, name):
        """Open a project's output folder or video on this PC (Explorer / Finder / default player)."""
        body = self._body()
        which = body.get("which", "output")
        jobs = self.app.jobs
        if which == "video":
            target = jobs.video_path(name)
        elif which == "package":
            target = self.app.cfg.folder("projects_dir") / name
        else:
            target = jobs.output_dir(name)
        if not target.exists():
            raise HttpError(404, "Not there yet: %s" % target)
        open_on_this_pc(str(target))
        return {"ok": True, "path": str(target)}

    def api_remove_project(self, name):
        """Move a project (and optionally its frames and video) to the trash folder."""
        body = self._body()
        a = self.app
        e = a.scanner.entry(name)
        if e is None:
            raise HttpError(404, "no project named %s" % name)
        a.jobs.forget_project(name)  # refuses while jobs are queued or running
        stamp = time.strftime("%Y%m%d-%H%M%S")
        trash = a.cfg.folder("projects_dir").parent / "trash" / ("%s-%s" % (stamp, name))
        trash.mkdir(parents=True, exist_ok=True)
        moved = []
        zips = a.scanner.source_zips(name)  # else the zip would be extracted again after a restart
        try:
            shutil.move(util.longpath(e["path"]), util.longpath(trash / os.path.basename(e["path"])))
            moved.append("project")
            for z in zips:
                shutil.move(util.longpath(z), util.longpath(trash / z.name))
                moved.append(z.name)
            out = a.jobs.output_dir(name)
            if body.get("with_output") and out.exists():
                shutil.move(util.longpath(out), util.longpath(trash / "output"))
                moved.append("frames and video")
        except OSError as ex:
            raise HttpError(409, "Could not move %s to the trash (%s). Close any program using its files and try again."
                            % (name, ex))
        finally:
            a.scanner.forget(name)
            a.jobs.devices.delete(name)
        util.log("Removed %s to %s (%s)" % (name, trash, ", ".join(moved)))
        return {"ok": True, "trash": str(trash), "moved": moved}


def open_on_this_pc(path):
    """Open a folder or file with the PC's own app (RenderHub runs on this same PC)."""
    import subprocess
    if util.IS_WINDOWS:
        os.startfile(path)  # noqa: (Windows only)
    elif util.IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def settings_warnings(app):
    w = []
    hw = app.hw.summary()
    n = app.cfg.get("max_concurrent_jobs")
    gpus = max(1, hw["gpu_count"]) if hw["chosen_backend"] not in (None, "CPU") else 1
    if n > gpus:
        w.append("Max concurrent jobs is %d but this PC has %d usable GPU%s. Two Blender jobs on one GPU ran out "
                 "of memory before; 1 per GPU is safer." % (n, gpus, "" if gpus == 1 else "s"))
    if app.cfg.get("device_override") not in ("auto", "CPU") and hw["chosen_backend"] is None:
        w.append("Device is set to %s, which this PC does not have." % app.cfg.get("device_override"))
    return w


NAME = r"([^/]+)"
ROUTES = [
    ("GET", r"/", Handler.page_index),
    ("GET", r"/index\.html", Handler.page_index),
    ("GET", r"/api/overview", Handler.api_overview),
    ("GET", r"/api/hardware", Handler.api_hardware),
    ("POST", r"/api/hardware/refresh", Handler.api_hardware_refresh),
    ("GET", r"/api/settings", Handler.api_settings),
    ("POST", r"/api/settings", Handler.api_settings_save),
    ("POST", r"/api/blender/refresh", Handler.api_blender_refresh),
    ("GET", r"/api/projects", Handler.api_projects),
    ("POST", r"/api/projects/rescan", Handler.api_rescan),
    ("GET", r"/api/projects/" + NAME, Handler.api_project),
    ("POST", r"/api/projects/" + NAME + r"/recheck", Handler.api_recheck),
    ("POST", r"/api/projects/" + NAME + r"/test-render", Handler.api_test_render),
    ("POST", r"/api/projects/" + NAME + r"/video", Handler.api_make_video),
    ("POST", r"/api/projects/" + NAME + r"/devices", Handler.api_devices),
    ("POST", r"/api/projects/" + NAME + r"/open-folder", Handler.api_open_folder),
    ("POST", r"/api/projects/" + NAME + r"/remove", Handler.api_remove_project),
    ("POST", r"/api/upload", Handler.api_upload),
    ("GET", r"/api/jobs", Handler.api_jobs),
    ("POST", r"/api/jobs", Handler.api_jobs_create),
    ("POST", r"/api/jobs/clear-finished", Handler.api_jobs_clear),
    ("POST", r"/api/jobs/" + NAME + r"/(cancel|resume|remove)", Handler.api_job_action),
    ("GET", r"/api/jobs/" + NAME + r"/log", Handler.api_job_log),
    ("GET", r"/files/output/(.+)", Handler.file_output),
]
