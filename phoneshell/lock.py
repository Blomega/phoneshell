"""One phone, one job.

Two processes driving one iPhone do not merely duplicate work, they corrupt
each other's measurements: process A terminates an app while process B is
mid-task, and B's check then reads a screen A produced. That is undetectable
after the fact, because a corrupted check produces an ordinary-looking pass or
fail rather than an error.

It has happened twice in this project. Once a stray alarm-cleaner stalled a run
for twenty minutes. The second time a watchdog resumed a sweep, the operator
started a second one believing the first was dead, and the two ran together for
seventy minutes before the duplicated task ids in the log gave it away. Sixty
four rows had to be thrown out.

So device-driving work takes an exclusive advisory lock. `flock` is the right
primitive here because the kernel releases it when the holder dies, however it
dies, which a pid file written by hand does not.

    with device_lock("sweep"):
        ...
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time
from pathlib import Path

from .config import RUNTIME

LOCK_PATH = RUNTIME / "device.lock"


class DeviceBusy(RuntimeError):
    """Something else already holds the phone."""


@contextlib.contextmanager
def device_lock(who: str = "", wait: float = 0.0):
    """Hold the phone exclusively, or raise DeviceBusy saying who has it.

    wait > 0 blocks for that many seconds before giving up, which suits a
    watchdog that expects the previous holder to be shutting down. The default
    of 0 fails immediately, which suits a human at a terminal who would rather
    be told than queued.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                handle.seek(0)
                holder = handle.read().strip() or "an unknown process"
                handle.close()
                raise DeviceBusy(
                    f"another process is already driving the phone: {holder}. "
                    f"Two processes on one device corrupt each other's checks, "
                    f"so this one is refusing to start."
                ) from None
            time.sleep(1.0)
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{who or 'phoneshell'} pid={os.getpid()} "
                     f"since={time.strftime('%H:%M:%S')}\n")
        handle.flush()
        yield
    finally:
        with contextlib.suppress(Exception):
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
