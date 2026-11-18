"""Make sure only one connector runs per settings folder.

The tray app starts automatically at sign-in; without this, double-clicking
the shortcut (or running ``keep-to-bring run`` in a terminal) would start a
second copy. The operating system releases the lock when the process exits,
even if it crashes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

LOCK_FILE_NAME = "keep-to-bring.lock"


class SingleInstance:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / LOCK_FILE_NAME
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        """Take the lock. False if another running copy holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - kept open while running
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()  # closing the file releases the lock
            self._handle = None

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
