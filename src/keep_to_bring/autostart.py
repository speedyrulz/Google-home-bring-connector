"""Start the tray app automatically when the user signs in.

Windows: a value under ``HKEY_CURRENT_USER\\...\\CurrentVersion\\Run`` (the
standard per-user startup list; no admin rights needed; it also shows up in
Task Manager > Startup apps). Linux desktops: an XDG autostart entry.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

APP_ID = "KeepToBring"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class Autostart(Protocol):
    label: str

    def registered_command(self) -> str | None: ...

    def command_for(self, args: Sequence[str]) -> str: ...

    def enable(self, args: Sequence[str]) -> None: ...

    def disable(self) -> None: ...


class WindowsAutostart:
    label = "Start with Windows"

    def __init__(self, winreg_module: Any = None) -> None:
        if winreg_module is None:
            import winreg as winreg_module  # type: ignore[no-redef]
        self._reg = winreg_module

    def command_for(self, args: Sequence[str]) -> str:
        return subprocess.list2cmdline(list(args))

    def registered_command(self) -> str | None:
        reg = self._reg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
                value, _kind = reg.QueryValueEx(key, APP_ID)
        except OSError:  # key or value missing
            return None
        return str(value)

    def enable(self, args: Sequence[str]) -> None:
        reg = self._reg
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, APP_ID, 0, reg.REG_SZ, self.command_for(args))

    def disable(self) -> None:
        reg = self._reg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, APP_ID)
        except OSError:
            pass


def _desktop_quote(arg: str) -> str:
    """Quote one argument for a .desktop Exec= line (freedesktop spec)."""
    if arg and re.fullmatch(r"[A-Za-z0-9_./:=+@%,-]+", arg):
        return arg
    escaped = re.sub(r'(["`$\\])', r"\\\1", arg)
    return f'"{escaped}"'


class XdgAutostart:
    label = "Start when I log in"

    def __init__(self, config_home: Path | None = None) -> None:
        base = config_home or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.path = Path(base) / "autostart" / "keep-to-bring.desktop"

    def command_for(self, args: Sequence[str]) -> str:
        return " ".join(_desktop_quote(arg) for arg in args)

    def registered_command(self) -> str | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("Exec="):
                return line[len("Exec="):]
        return None

    def enable(self, args: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Keep to Bring\n"
            "Comment=Copy Google Home shopping-list items to Bring!\n"
            f"Exec={self.command_for(args)}\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )

    def disable(self) -> None:
        self.path.unlink(missing_ok=True)


def for_this_platform() -> Autostart | None:
    if sys.platform == "win32":
        return WindowsAutostart()
    if sys.platform.startswith("linux"):
        return XdgAutostart()
    return None
