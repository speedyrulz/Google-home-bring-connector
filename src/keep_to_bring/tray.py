"""System tray app: runs the connector in the background without a console window.

Windows is the main target (Linux desktops work too). The basket icon's colour
shows the status; right-click it for options. On first start the app adds
itself to the user's startup programs and creates a Start-menu shortcut.

Start it with ``keep-to-bring tray`` (which detaches from the terminal), or the
windowless ``keep-to-bring-tray`` program that autostart uses.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .autostart import Autostart, for_this_platform
from .config import ConfigError, load_environment, load_settings, resolve_env_file
from .service import (
    ERROR,
    OK,
    STARTING,
    STOPPED,
    WARNING,
    ConnectorService,
    ServiceStatus,
    account_reloader,
    quiet_noisy_loggers,
)
from .single_instance import SingleInstance

_LOGGER = logging.getLogger(__name__)

APP_TITLE = "Keep \u2192 Bring"
LOG_FILE_NAME = "keep-to-bring.log"
PREFS_FILE_NAME = "tray-settings.json"
ICON_FILE_NAME = "keep-to-bring.ico"
SHORTCUT_NAME = "Keep to Bring.lnk"
POLL_SECONDS = 2.0
# Windows limits for tray texts (characters, excluding the terminating NUL).
TOOLTIP_MAX, NOTIFY_MAX, NOTIFY_TITLE_MAX = 127, 255, 63

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

STATE_COLORS = {
    STARTING: (117, 117, 117),
    OK: (46, 125, 50),
    WARNING: (230, 124, 0),
    ERROR: (198, 40, 40),
    STOPPED: (117, 117, 117),
}


def is_windows() -> bool:
    return sys.platform == "win32"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# ----------------------------------------------------------------------
# Icon
# ----------------------------------------------------------------------


def make_icon_image(state: str, size: int = 64) -> Any:
    """A white shopping basket on a rounded square in the status colour."""
    from PIL import Image, ImageDraw

    scale = 4  # draw large, then shrink, for smooth edges
    w = size * scale
    color = STATE_COLORS.get(state, STATE_COLORS[STARTING]) + (255,)
    white = (255, 255, 255, 255)
    image = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, w - 1, w - 1), radius=int(w * 0.22), fill=color)
    # Handle, rim, body, and slats.
    draw.arc((w * 0.30, w * 0.16, w * 0.70, w * 0.58), start=180, end=360, fill=white, width=max(1, int(w * 0.07)))
    draw.rounded_rectangle((w * 0.15, w * 0.37, w * 0.85, w * 0.48), radius=int(w * 0.03), fill=white)
    draw.polygon([(w * 0.21, w * 0.50), (w * 0.79, w * 0.50), (w * 0.70, w * 0.83), (w * 0.30, w * 0.83)], fill=white)
    for x in (0.39, 0.50, 0.61):
        draw.line([(w * x, w * 0.56), (w * x, w * 0.77)], fill=color, width=max(1, int(w * 0.035)))
    return image.resize((size, size), Image.LANCZOS)


# ----------------------------------------------------------------------
# Small persistent preferences (separate from the .env settings)
# ----------------------------------------------------------------------


@dataclass
class TrayPrefs:
    autostart_opt_out: bool = False  # user unticked "Start with Windows"
    notify_items: bool = False  # show a notification for every added item
    first_run_done: bool = False
    setup_offered: bool = False  # setup window opened automatically once

    @classmethod
    def load(cls, path: Path) -> TrayPrefs:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: bool(v) for k, v in raw.items() if k in known}) if isinstance(raw, dict) else cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        except OSError as err:
            _LOGGER.warning("Could not save %s: %s", path, err)


# ----------------------------------------------------------------------
# Commands used to launch things
# ----------------------------------------------------------------------


def _scripts_dirs() -> list[Path]:
    dirs = [Path(sys.executable).parent]
    for scheme in (None, f"{os.name}_user"):
        try:
            path = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except KeyError:
            continue
        if path:
            dirs.append(Path(path))
    unique: list[Path] = []
    for directory in dirs:
        if directory not in unique:
            unique.append(directory)
    return unique


def python_executable(windowed: bool) -> str:
    """python.exe (with console) or pythonw.exe (without) from this installation."""
    exe = Path(sys.executable)
    if is_windows():
        candidate = exe.with_name("pythonw.exe" if windowed else "python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def launcher_command(env_file: Path) -> list[str]:
    """How to start the tray app without a console (used for autostart and the shortcut)."""
    name = "keep-to-bring-tray.exe" if is_windows() else "keep-to-bring-tray"
    for directory in _scripts_dirs():
        if (directory / name).is_file():
            return [str(directory / name), "--env-file", str(env_file)]
    return [python_executable(windowed=True), "-m", "keep_to_bring.tray", "--env-file", str(env_file)]


def console_command(env_file: Path, *command: str) -> list[str]:
    """A CLI command that runs in its own console window and waits for Enter at the end."""
    return [python_executable(windowed=False), "-m", "keep_to_bring", "--env-file", str(env_file), "--pause", *command]


def tray_data_dir(env_file: Path) -> Path:
    """DATA_DIR from the settings (relative paths are relative to the settings file)."""
    env = load_environment(env_file)
    data_dir = Path(env.get("DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = env_file.parent / data_dir
    return data_dir


# ----------------------------------------------------------------------
# Desktop integration (swappable for tests)
# ----------------------------------------------------------------------


class Desktop:
    """Opening files and windows on the user's desktop."""

    def open_in_editor(self, path: Path) -> None:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_console(self, args: Sequence[str]) -> bool:
        if is_windows():
            subprocess.Popen(list(args), creationflags=CREATE_NEW_CONSOLE)
            return True
        for terminal in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
            if shutil.which(terminal[0]):
                subprocess.Popen(terminal + list(args))
                return True
        return False

    def create_start_menu_shortcut(self, command: Sequence[str], icon: Path, workdir: Path) -> None:
        """Windows: add 'Keep to Bring' to the Start menu (no-op elsewhere or if present)."""
        if not is_windows() or not os.environ.get("APPDATA"):
            return
        link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME
        if link.exists():
            return
        # Values travel in environment variables, so no quoting problems.
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:K2B_LINK); "
            "$s.TargetPath = $env:K2B_TARGET; $s.Arguments = $env:K2B_ARGS; "
            "$s.WorkingDirectory = $env:K2B_WORKDIR; $s.IconLocation = $env:K2B_ICON; "
            "$s.Description = 'Copy Google Home shopping-list items to Bring!'; $s.Save()"
        )
        env = dict(
            os.environ,
            K2B_LINK=str(link),
            K2B_TARGET=command[0],
            K2B_ARGS=subprocess.list2cmdline(list(command[1:])),
            K2B_WORKDIR=str(workdir),
            K2B_ICON=str(icon),
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env,
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            timeout=60,
            check=True,
        )
        _LOGGER.info("Added a Start menu shortcut: %s", link)


def show_message_box(title: str, text: str) -> None:
    """A simple message for situations where there is no tray icon yet."""
    if is_windows():
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x40000)  # info icon, topmost
    elif sys.stderr is not None:
        print(f"{title}: {text}", file=sys.stderr)


# ----------------------------------------------------------------------
# Controller: everything the tray does, without any GUI code
# ----------------------------------------------------------------------


class TrayView(Protocol):
    def refresh(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...


class _NullView:
    def refresh(self) -> None:
        pass

    def notify(self, message: str, title: str | None = None) -> None:
        _LOGGER.info("Notification: %s", message)


class TrayController:
    def __init__(
        self,
        env_file: Path,
        data_dir: Path,
        *,
        autostart: Autostart | None,
        launcher: Sequence[str],
        service_factory: Callable[..., ConnectorService] = ConnectorService,
        desktop: Desktop | None = None,
    ) -> None:
        self.env_file = env_file
        self.data_dir = data_dir
        self.log_file = data_dir / LOG_FILE_NAME
        self.prefs_path = data_dir / PREFS_FILE_NAME
        self.prefs = TrayPrefs.load(self.prefs_path)
        self.autostart = autostart
        self.launcher = list(launcher)
        self.desktop = desktop or Desktop()
        self.view: TrayView = _NullView()
        self.service: ConnectorService | None = None
        self.status = ServiceStatus()
        self._service_factory = service_factory
        self._env_signature: tuple[int, int] | None = None
        self._had_problem = False
        self._lock = threading.RLock()

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        first_run = not self.prefs.first_run_done
        self._ensure_autostart()
        self._ensure_shortcut()
        self._reload()
        if first_run:
            self.prefs.first_run_done = True
            self.prefs.save(self.prefs_path)
            if self.status.state != ERROR:
                extra = " It starts automatically when you sign in." if self.autostart_enabled() else ""
                self.view.notify(f"Running in the background - this basket icon shows its status.{extra}")

    def poll(self) -> None:
        """Called every few seconds: picks up edits to the settings file."""
        if self._signature() != self._env_signature:
            _LOGGER.info("Settings file changed; reloading")
            self._reload()

    def shutdown(self) -> None:
        with self._lock:
            self._stop_service()

    # --- settings and service -------------------------------------------

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.env_file.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload(self) -> None:
        with self._lock:
            self._env_signature = self._signature()
            try:
                settings = load_settings(load_environment(self.env_file))
            except ConfigError as err:
                self._stop_service()
                missing = not self.env_file.is_file()
                _LOGGER.error("Settings problem in %s:\n%s", self.env_file, err)
                message = (
                    "Not set up yet - right-click and choose Run setup"
                    if missing
                    else "Settings problem - right-click and choose Open settings file"
                )
                self._set_status(ServiceStatus(state=ERROR, message=message))
                if missing and not self.prefs.setup_offered:
                    self.prefs.setup_offered = True
                    self.prefs.save(self.prefs_path)
                    self.run_setup()
                return
            self._stop_service()
            self._set_status(ServiceStatus())
            holder: dict[str, ConnectorService] = {}

            def on_update(status: ServiceStatus) -> None:
                if holder.get("service") is self.service:  # ignore a replaced service
                    self._on_service_update(status)

            service = self._service_factory(
                settings, on_update=on_update, reload_accounts=account_reloader(self.env_file)
            )
            holder["service"] = service
            self.service = service
            service.start()
            _LOGGER.info(
                "Connector started: %s -> Bring!",
                ", ".join(a.email for a in settings.google_accounts),
            )

    def _stop_service(self) -> None:
        service, self.service = self.service, None
        if service is not None:
            service.stop()

    def _set_status(self, status: ServiceStatus) -> None:
        self.status = status
        self.view.refresh()

    def _on_service_update(self, status: ServiceStatus) -> None:
        previous = self.status
        self._set_status(status)
        if status.state == ERROR and previous.state != ERROR:
            self._had_problem = True
            self.view.notify(f"{status.message} {self._hint(status.message)}", f"{APP_TITLE} needs attention")
        elif status.state == OK and self._had_problem:
            self._had_problem = False
            self.view.notify("Working again.")
        if status.new_items and self.prefs.notify_items:
            self.view.notify("Added to Bring!: " + ", ".join(status.new_items))

    @staticmethod
    def _hint(message: str) -> str:
        if "Google login" in message:
            return "Right-click the basket icon and choose Renew Google login."
        if "Bring!" in message:
            return "Check your Bring! details: right-click the basket icon and choose Run setup again."
        return "Right-click the basket icon and choose Open log for details."

    # --- what the icon shows -------------------------------------------

    def icon_state(self) -> str:
        return self.status.state

    def status_line(self) -> str:
        status = self.status
        if status.last_check and status.state in (OK, WARNING):
            return f"{status.message} (checked {status.last_check:%H:%M})"
        return status.message

    def last_items_line(self) -> str | None:
        if not self.status.recent_items:
            return None
        return clip("Last added: " + ", ".join(reversed(self.status.recent_items[-3:])), 60)

    def tooltip(self) -> str:
        return clip(f"{APP_TITLE}: {self.status_line()}", TOOLTIP_MAX)

    def autostart_enabled(self) -> bool:
        return self.autostart is not None and self.autostart.registered_command() is not None

    # --- menu actions -----------------------------------------------------

    def sync_now(self) -> None:
        if self.service is not None and self.service.running:
            self.service.sync_now()
        else:
            self._reload()

    def open_log(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.touch(exist_ok=True)
        self.desktop.open_in_editor(self.log_file)

    def open_settings(self) -> None:
        if self.env_file.is_file():
            self.desktop.open_in_editor(self.env_file)
        else:
            self.run_setup()

    def run_setup(self) -> None:
        self._open_console("setup")

    def renew_google_login(self) -> None:
        self._open_console("get-token")

    def _open_console(self, command: str) -> None:
        if not self.desktop.open_console(console_command(self.env_file, command)):
            self.view.notify(f"Open a terminal in {self.env_file.parent} and run: keep-to-bring {command}")

    def toggle_autostart(self) -> None:
        if self.autostart is None:
            return
        try:
            if self.autostart_enabled():
                self.autostart.disable()
                self.prefs.autostart_opt_out = True
            else:
                self.autostart.enable(self.launcher)
                self.prefs.autostart_opt_out = False
        except OSError as err:
            _LOGGER.warning("Could not change automatic start: %s", err)
        self.prefs.save(self.prefs_path)
        self.view.refresh()

    def toggle_notify_items(self) -> None:
        self.prefs.notify_items = not self.prefs.notify_items
        self.prefs.save(self.prefs_path)
        self.view.refresh()

    # --- first-run integration ---------------------------------------------

    def _ensure_autostart(self) -> None:
        if self.autostart is None or self.prefs.autostart_opt_out:
            return
        wanted = self.autostart.command_for(self.launcher)
        if self.autostart.registered_command() == wanted:
            return
        try:
            self.autostart.enable(self.launcher)
            _LOGGER.info("Set to start automatically: %s", wanted)
        except OSError as err:
            _LOGGER.warning("Could not set up automatic start: %s", err)

    def _ensure_shortcut(self) -> None:
        if not is_windows():
            return
        try:
            icon_path = self.data_dir / ICON_FILE_NAME
            if not icon_path.exists():
                sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
                make_icon_image(OK, 256).save(icon_path, sizes=sizes)
            self.desktop.create_start_menu_shortcut(self.launcher, icon_path, self.env_file.parent)
        except Exception as err:  # noqa: BLE001 - a missing shortcut is not worth failing over
            _LOGGER.warning("Could not create the Start menu shortcut: %s", err)


# ----------------------------------------------------------------------
# The actual tray icon (pystray)
# ----------------------------------------------------------------------


def _menu_text(text: str) -> str:
    # "&" marks a keyboard shortcut in Windows menus; "&&" shows a literal "&".
    return text.replace("&", "&&") if is_windows() else text


class TrayApp:
    def __init__(self, controller: TrayController) -> None:
        import pystray

        self._pystray = pystray
        self.controller = controller
        self._stopping = threading.Event()
        self._images: dict[str, Any] = {}
        self.icon = pystray.Icon(
            "keep-to-bring", icon=self._image(controller.icon_state()), title=APP_TITLE, menu=self._build_menu()
        )
        controller.view = self

    def _image(self, state: str) -> Any:
        if state not in self._images:
            self._images[state] = make_icon_image(state)
        return self._images[state]

    def _build_menu(self) -> Any:
        item, menu = self._pystray.MenuItem, self._pystray.Menu
        c = self.controller
        autostart_label = c.autostart.label if c.autostart is not None else "Start automatically"
        return menu(
            item(lambda _item: _menu_text(c.status_line()), None, enabled=False),
            item(
                lambda _item: _menu_text(c.last_items_line() or ""),
                None,
                enabled=False,
                visible=lambda _item: c.last_items_line() is not None,
            ),
            menu.SEPARATOR,
            item("Sync now", lambda: c.sync_now(), default=True),
            item("Renew Google login\u2026", lambda: c.renew_google_login()),
            item("Run setup again\u2026", lambda: c.run_setup()),
            item("Open settings file", lambda: c.open_settings()),
            item("Open log", lambda: c.open_log()),
            menu.SEPARATOR,
            item(
                autostart_label,
                lambda: c.toggle_autostart(),
                checked=lambda _item: c.autostart_enabled(),
                visible=c.autostart is not None,
            ),
            item(
                "Notify me when items are added",
                lambda: c.toggle_notify_items(),
                checked=lambda _item: c.prefs.notify_items,
            ),
            menu.SEPARATOR,
            item("Quit", lambda: self.quit()),
        )

    # TrayView ----------------------------------------------------------

    def refresh(self) -> None:
        try:
            self.icon.icon = self._image(self.controller.icon_state())
            self.icon.title = self.controller.tooltip()
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 - cosmetic; never break syncing over it
            _LOGGER.debug("Could not refresh the tray icon", exc_info=True)

    def notify(self, message: str, title: str | None = None) -> None:
        _LOGGER.info("Notification: %s", message)
        if not getattr(self.icon, "HAS_NOTIFICATION", False):
            return
        try:
            self.icon.notify(clip(message, NOTIFY_MAX), clip(title or APP_TITLE, NOTIFY_TITLE_MAX))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not show a notification", exc_info=True)

    # Running -----------------------------------------------------------

    def run(self) -> None:
        def setup(icon: Any) -> None:
            try:
                icon.visible = True
            except Exception:  # noqa: BLE001 - keep syncing even if the icon can't be shown
                _LOGGER.exception("Could not show the tray icon")
            try:
                self.controller.start()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Could not start the connector")
            self.refresh()
            while not self._stopping.wait(POLL_SECONDS):
                try:
                    self.controller.poll()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error while checking the settings file")

        self.icon.run(setup=setup)

    def quit(self) -> None:
        _LOGGER.info("Quitting")
        self._stopping.set()
        try:
            self.icon.visible = False
        except Exception:  # noqa: BLE001
            pass
        self.controller.shutdown()
        self.icon.stop()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def setup_file_logging(data_dir: Path, level: str) -> Path:
    """Log to a rotating file - there is no console to log to."""
    # pythonw.exe has no stdout/stderr; give libraries somewhere harmless to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / LOG_FILE_NAME
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    quiet_noisy_loggers(level)
    sys.excepthook = lambda kind, value, tb: _LOGGER.critical("Unhandled error", exc_info=(kind, value, tb))
    threading.excepthook = lambda a: _LOGGER.critical(
        "Unhandled error in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="keep-to-bring-tray",
        description="Run the Keep-to-Bring connector with a system tray icon.",
    )
    parser.add_argument("--env-file", help="settings file (default: $KEEP_TO_BRING_ENV_FILE or ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    env_file = resolve_env_file(args.env_file).resolve()
    os.chdir(env_file.parent)  # relative paths in the settings are relative to the settings file
    data_dir = tray_data_dir(env_file)
    level = "DEBUG" if args.verbose else (load_environment(env_file).get("LOG_LEVEL") or "INFO").upper()
    setup_file_logging(data_dir, level)

    lock = SingleInstance(data_dir)
    if not lock.acquire():
        show_message_box(
            APP_TITLE,
            f"{APP_TITLE} is already running.\n\nLook for the basket icon in the system tray "
            "(you may need to click the ^ arrow next to the clock).",
        )
        return 0
    try:
        controller = TrayController(
            env_file, data_dir, autostart=for_this_platform(), launcher=launcher_command(env_file)
        )
        app = TrayApp(controller)
    except ImportError as err:
        _LOGGER.error("Tray icon support is not installed: %s", err)
        show_message_box(APP_TITLE, f"The tray icon needs two extra packages. Run:\n\npip install pystray Pillow\n\n({err})")
        lock.release()
        return 1
    _LOGGER.info("Keep-to-Bring %s tray started (settings: %s)", __version__, env_file)
    try:
        app.run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
