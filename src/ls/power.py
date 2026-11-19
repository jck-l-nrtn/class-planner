"""Power state, for jobs that launchd starts while the lid is closed."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

MAC = sys.platform == "darwin"


def full_wake() -> bool:
    """False only when macOS reports a dark wake (display side powered down)."""
    if not MAC:
        return True
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "systemstate"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    line = next((ln for ln in out.splitlines() if "Capabilities" in ln), "")
    return not line or "Graphics" in line


@contextlib.contextmanager
def keep_awake():
    """Ask the Mac to stay up while the block runs."""
    proc = None
    if MAC:
        try:
            proc = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError:
            proc = None
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class Awake:
    """Seconds this process has actually been running, ignoring time asleep."""

    def __init__(self, cap: float = 6.0, clock=time.time):
        self._clock, self._cap = clock, cap
        self._last = clock()
        self.total = 0.0

    def tick(self) -> float:
        now = self._clock()
        self.total += min(max(now - self._last, 0.0), self._cap)
        self._last = now
        return self.total


def wait_until(cond, awake_seconds: float, poll: float = 2.0, sleep=time.sleep) -> bool:
    """Poll cond() for up to `awake_seconds` of running time."""
    if cond():
        return True
    awake = Awake(cap=poll * 3)
    while awake.tick() < awake_seconds:
        sleep(poll)
        if cond():
            return True
    return False


def wait_for_wake(seconds: float, poll: float = 5.0, sleep=time.sleep,
                  clock=time.time) -> bool:
    """Wait up to `seconds` of real time for someone to open the lid."""
    end = clock() + seconds
    while True:
        if full_wake():
            return True
        if clock() >= end:
            return False
        sleep(poll)
