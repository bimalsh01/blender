"""Keep the PC awake while jobs run. Call set_active() only from one long-lived thread
(the job runner): on Windows the request belongs to the calling thread."""

import os
import shutil
import subprocess
import sys

from . import util

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class SleepGuard:
    def __init__(self):
        self.active = False
        self.proc = None

    def set_active(self, want):
        if want == self.active:
            return
        try:
            if want:
                self._acquire()
            else:
                self._release()
            self.active = want
            util.log("Keep-awake %s." % ("on" if want else "off"))
        except Exception as e:
            util.log("Keep-awake could not be %s: %s" % ("enabled" if want else "released", e))
            self.active = want

    def _acquire(self):
        if util.IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        elif util.IS_MAC:
            self.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
            self.proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=RenderHub",
                                          "--why=Rendering", "sleep", "2147483647"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _release(self):
        if util.IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
