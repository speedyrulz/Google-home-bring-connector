"""Tray app tests. pystray runs with its display-free "dummy" backend."""

import os

os.environ.setdefault("PYSTRAY_BACKEND", "dummy")  # must be set before pystray is imported

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

from keep_to_bring import tray
from keep_to_bring.service import ERROR, OK, STARTING, WARNING, ServiceStatus
from keep_to_bring.single_instance import SingleInstance
from keep_to_bring.tray import (
    PREFS_FILE_NAME,
    STATE_COLORS,
    TOOLTIP_MAX,
    Desktop,
    TrayApp,
    TrayController,
    TrayPrefs,
    console_command,
    launcher_command,
    make_icon_image,
    tray_data_dir,
)

VALID_ENV = (
    "GOOGLE_EMAIL=me@example.com\n"
    "GOOGLE_MASTER_TOKEN=aas_et/abc\n"
    "BRING_EMAIL=bring@example.com\n"
    "BRING_PASSWORD=pw\n"
)


class FakeAutostart:
    label = "Start with Windows"

    def __init__(self, command=None):
        self.command = command

    def command_for(self, args):
        return " ".join(args)

    def registered_command(self):
        return self.command

    def enable(self, args):
        self.command = self.command_for(args)

    def disable(self):
        self.command = None


class FakeDesktop(Desktop):
    def __init__(self):
        self.opened, self.consoles, self.shortcuts = [], [], []
        self.console_works = True

    def open_in_editor(self, path):
        self.opened.append(Path(path))

    def open_console(self, args):
        self.consoles.append(list(args))
        return self.console_works

    def create_start_menu_shortcut(self, command, icon, workdir):
        self.shortcuts.append((list(command), icon, workdir))


class FakeService:
    def __init__(self, settings, *, on_update, reload_accounts):
        self.settings, self.on_update, self.reload_accounts = settings, on_update, reload_accounts
        self.started = self.stopped = False
        self.synced = 0

    @property
    def running(self):
        return self.started and not self.stopped

    def start(self):
        self.started = True

    def sync_now(self):
        self.synced += 1

    def stop(self, timeout=15.0):
        self.stopped = True
        return True


class RecordingView:
    def __init__(self):
        self.refreshes = 0
        self.notes = []

    def refresh(self):
        self.refreshes += 1

    def notify(self, message, title=None):
        self.notes.append((message, title))


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / ".env"


@pytest.fixture
def make_controller(env_file, tmp_path):
    created = []

    def make(autostart=None, desktop=None):
        controller = TrayController(
            env_file,
            tmp_path / "data",
            autostart=autostart if autostart is not None else FakeAutostart(),
            launcher=["/opt/keep-to-bring-tray", "--env-file", str(env_file)],
            service_factory=FakeService,
            desktop=desktop or FakeDesktop(),
        )
        controller.view = RecordingView()
        created.append(controller)
        return controller

    yield make
    for controller in created:
        controller.shutdown()


def touch_env(path, text):
    path.write_text(text, encoding="utf-8")
    # Make sure the change is visible even on filesystems with coarse timestamps.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


# ----------------------------------------------------------------------
# Icon
# ----------------------------------------------------------------------


@pytest.mark.parametrize("state", [STARTING, OK, WARNING, ERROR])
def test_icon_images(state):
    image = make_icon_image(state)
    assert image.size == (64, 64) and image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0  # rounded, transparent corner
    background = image.getpixel((32, 4))[:3]
    assert all(abs(a - b) <= 3 for a, b in zip(background, STATE_COLORS[state]))
    assert image.getpixel((32, 27))[:3] == (255, 255, 255)  # basket rim
    assert make_icon_image(state, 16).size == (16, 16)


# ----------------------------------------------------------------------
# Controller
# ----------------------------------------------------------------------


def test_first_start_runs_connector_and_registers_autostart(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    assert controller.service.started
    assert controller.service.settings.google_accounts[0].email == "me@example.com"
    assert controller.status.state == STARTING
    assert controller.autostart.command == " ".join(controller.launcher)
    assert controller.autostart_enabled()
    assert controller.prefs.first_run_done
    (message, _title), = controller.view.notes
    assert "starts automatically" in message

    # Second start: no welcome message again.
    again = make_controller()
    again.start()
    assert again.view.notes == []


def test_autostart_path_is_updated_after_moving_the_folder(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller(autostart=FakeAutostart("C:/old/place/keep-to-bring-tray.exe"))
    controller.start()
    assert controller.autostart.command == " ".join(controller.launcher)


def test_turning_autostart_off_is_remembered(make_controller, env_file, tmp_path):
    env_file.write_text(VALID_ENV)
    autostart = FakeAutostart()
    controller = make_controller(autostart=autostart)
    controller.start()
    controller.toggle_autostart()
    assert not controller.autostart_enabled()
    assert TrayPrefs.load(tmp_path / "data" / PREFS_FILE_NAME).autostart_opt_out

    # Next launch respects the choice...
    make_controller(autostart=autostart).start()
    assert autostart.command is None
    # ...until it is turned back on.
    controller.toggle_autostart()
    assert controller.autostart_enabled()
    assert not controller.prefs.autostart_opt_out


def test_no_autostart_support(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = TrayController(
        env_file, env_file.parent / "data", autostart=None, launcher=["x"],
        service_factory=FakeService, desktop=FakeDesktop(),
    )
    controller.start()
    assert not controller.autostart_enabled()
    controller.toggle_autostart()  # no-op
    controller.shutdown()


def test_missing_settings_opens_setup_once(make_controller, env_file):
    controller = make_controller()
    controller.start()
    assert controller.service is None
    assert controller.status.state == ERROR
    assert "Run setup" in controller.status.message
    (command,) = controller.desktop.consoles
    assert command[-2:] == ["--pause", "setup"]
    assert str(env_file) in command

    later = make_controller()
    later.start()
    assert later.desktop.consoles == []  # not pushed on the user every time


def test_settings_created_later_starts_the_connector(make_controller, env_file):
    controller = make_controller()
    controller.start()
    controller.poll()
    assert controller.service is None
    touch_env(env_file, VALID_ENV)
    controller.poll()
    assert controller.service is not None and controller.service.started
    assert controller.status.state == STARTING


def test_edited_settings_restart_the_connector(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    first = controller.service
    controller.poll()
    assert controller.service is first  # unchanged file: nothing happens
    touch_env(env_file, VALID_ENV + "POLL_INTERVAL_SECONDS=30\n")
    controller.poll()
    assert first.stopped
    assert controller.service is not first
    assert controller.service.settings.poll_interval == 30


def test_broken_settings_are_reported(make_controller, env_file):
    env_file.write_text("GOOGLE_EMAIL=me@example.com\n")
    controller = make_controller()
    controller.start()
    assert controller.status.state == ERROR
    assert "Open settings file" in controller.status.message
    assert controller.desktop.consoles == []


def test_status_changes_and_notifications(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    view, service = controller.view, controller.service
    view.notes.clear()

    service.on_update(ServiceStatus(state=OK, message="Working", last_check=datetime(2026, 1, 1, 14, 32)))
    assert controller.icon_state() == OK
    assert controller.status_line() == "Working (checked 14:32)"
    assert view.notes == []

    google = ServiceStatus(state=ERROR, message="Google login needs renewing for me@example.com.")
    service.on_update(google)
    (message, title), = view.notes
    assert "Renew Google login" in message and "needs attention" in title
    service.on_update(google)
    assert len(view.notes) == 1  # no repeat while the problem lasts

    service.on_update(ServiceStatus(state=OK, message="Working"))
    assert view.notes[-1][0] == "Working again."

    service.on_update(ServiceStatus(state=OK, message="Working", new_items=("Milk",), recent_items=("Milk",)))
    assert len(view.notes) == 2  # item notifications are off by default
    controller.toggle_notify_items()
    service.on_update(ServiceStatus(state=OK, message="Working", new_items=("Eggs",), recent_items=("Milk", "Eggs")))
    assert view.notes[-1][0] == "Added to Bring!: Eggs"
    assert controller.last_items_line() == "Last added: Eggs, Milk"


def test_updates_from_a_replaced_service_are_ignored(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    old = controller.service
    touch_env(env_file, VALID_ENV + "LOG_LEVEL=DEBUG\n")
    controller.poll()
    old.on_update(ServiceStatus(state=ERROR, message="old news"))
    assert controller.status.state == STARTING


def test_menu_actions(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    desktop = controller.desktop

    controller.sync_now()
    assert controller.service.synced == 1

    controller.open_log()
    assert desktop.opened[-1] == controller.log_file and controller.log_file.exists()
    controller.open_settings()
    assert desktop.opened[-1] == env_file

    controller.renew_google_login()
    assert desktop.consoles[-1][-2:] == ["--pause", "get-token"]
    controller.run_setup()
    assert desktop.consoles[-1][-2:] == ["--pause", "setup"]

    desktop.console_works = False
    controller.renew_google_login()
    assert "keep-to-bring get-token" in controller.view.notes[-1][0]


def test_sync_now_restarts_a_stopped_connector(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    first = controller.service
    first.stopped = True
    controller.sync_now()
    assert controller.service is not first and controller.service.started


def test_tooltip_is_short_enough_for_windows(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    controller.start()
    controller.service.on_update(ServiceStatus(state=ERROR, message="x" * 500))
    assert len(controller.tooltip()) <= TOOLTIP_MAX
    assert controller.tooltip().endswith("\u2026")


def test_start_menu_shortcut_only_on_windows(make_controller, env_file, monkeypatch):
    env_file.write_text(VALID_ENV)
    monkeypatch.setattr(tray, "is_windows", lambda: False)
    controller = make_controller()
    controller.start()
    assert controller.desktop.shortcuts == []

    monkeypatch.setattr(tray, "is_windows", lambda: True)
    controller = make_controller()
    controller.start()
    (command, icon, workdir), = controller.desktop.shortcuts
    assert command == controller.launcher
    assert icon.exists() and icon.suffix == ".ico"
    assert workdir == env_file.parent


def test_prefs_survive_bad_files(tmp_path):
    path = tmp_path / PREFS_FILE_NAME
    path.write_text("not json")
    assert TrayPrefs.load(path) == TrayPrefs()
    path.write_text('{"notify_items": true, "unknown": 1}')
    assert TrayPrefs.load(path) == TrayPrefs(notify_items=True)
    path.write_text("[1, 2]")
    assert TrayPrefs.load(path) == TrayPrefs()


# ----------------------------------------------------------------------
# pystray menu
# ----------------------------------------------------------------------


def menu_items(app):
    return {str(item.text): item for item in app.icon.menu.items if item.text}


def test_tray_menu(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    app = TrayApp(controller)
    assert controller.view is app
    controller.start()

    items = menu_items(app)
    assert list(items)[0] == "Starting..."  # the status line comes first
    assert not items["Starting..."].enabled
    assert items["Sync now"].default
    assert items["Start with Windows"].checked is True
    assert items["Notify me when items are added"].checked is False

    items["Sync now"](app.icon)
    assert controller.service.synced == 1
    items["Notify me when items are added"](app.icon)
    assert controller.prefs.notify_items
    items["Start with Windows"](app.icon)
    assert not controller.autostart_enabled()

    app.refresh()  # must not raise even though the dummy backend can't draw
    app.notify("hello")

    service = controller.service
    items["Quit"](app.icon)
    assert service.stopped


def test_last_added_line_appears_once_there_are_items(make_controller, env_file):
    env_file.write_text(VALID_ENV)
    controller = make_controller()
    app = TrayApp(controller)
    controller.start()
    assert not any(str(i.text).startswith("Last added") for i in app.icon.menu.items if i.visible)
    controller.service.on_update(ServiceStatus(state=OK, message="Working", recent_items=("Milk",)))
    visible = [str(i.text) for i in app.icon.menu.items if i.visible]
    assert "Last added: Milk" in visible


def test_ampersands_are_escaped_in_windows_menus(monkeypatch):
    monkeypatch.setattr(tray, "is_windows", lambda: True)
    assert tray._menu_text("Last added: Mac & cheese") == "Last added: Mac && cheese"


# ----------------------------------------------------------------------
# Launch commands and folders
# ----------------------------------------------------------------------


def test_launcher_prefers_the_windowless_program(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(tray, "_scripts_dirs", lambda: [tmp_path / "missing", tmp_path])
    assert launcher_command(env)[1:] == ["-m", "keep_to_bring.tray", "--env-file", str(env)]
    (tmp_path / "keep-to-bring-tray").write_text("")
    assert launcher_command(env) == [str(tmp_path / "keep-to-bring-tray"), "--env-file", str(env)]


def test_windows_launch_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "is_windows", lambda: True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    for name in ("python.exe", "pythonw.exe"):
        (tmp_path / name).write_text("")
    env = tmp_path / ".env"
    monkeypatch.setattr(tray, "_scripts_dirs", lambda: [tmp_path])
    # No launcher exe: fall back to pythonw (no console window).
    assert launcher_command(env)[0] == str(tmp_path / "pythonw.exe")
    (tmp_path / "keep-to-bring-tray.exe").write_text("")
    assert launcher_command(env)[0] == str(tmp_path / "keep-to-bring-tray.exe")
    # Setup must run in a real console, and wait for Enter at the end.
    command = console_command(env, "setup")
    assert command[0] == str(tmp_path / "python.exe")
    assert command[1:] == ["-m", "keep_to_bring", "--env-file", str(env), "--pause", "setup"]


def test_scripts_dirs_includes_this_python():
    assert Path(sys.executable).parent in tray._scripts_dirs()


def test_tray_data_dir(tmp_path):
    env = tmp_path / ".env"
    assert tray_data_dir(env) == tmp_path / "data"
    env.write_text("DATA_DIR=state\n")
    assert tray_data_dir(env) == tmp_path / "state"
    env.write_text(f"DATA_DIR={tmp_path / 'abs'}\n")
    assert tray_data_dir(env) == tmp_path / "abs"


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


@pytest.fixture
def isolated_process_state(monkeypatch, tmp_path):
    """tray.main() changes directory, logging and exception hooks: undo that."""
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in handlers:
            handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_main_refuses_a_second_copy(tmp_path, monkeypatch, isolated_process_state):
    env = tmp_path / ".env"
    env.write_text(VALID_ENV)
    messages = []
    monkeypatch.setattr(tray, "show_message_box", lambda title, text: messages.append(text))
    holder = SingleInstance(tmp_path / "data")
    assert holder.acquire()
    try:
        assert tray.main(["--env-file", str(env)]) == 0
    finally:
        holder.release()
    assert "already running" in messages[0]


def test_main_explains_missing_packages(tmp_path, monkeypatch, isolated_process_state):
    env = tmp_path / ".env"
    messages = []
    monkeypatch.setattr(tray, "show_message_box", lambda title, text: messages.append(text))

    def no_pystray(controller):
        raise ImportError("No module named 'pystray'")

    monkeypatch.setattr(tray, "TrayApp", no_pystray)
    assert tray.main(["--env-file", str(env)]) == 1
    assert "pip install pystray Pillow" in messages[0]
    assert SingleInstance(tmp_path / "data").acquire()  # lock was released


def test_main_runs_the_app_and_logs_to_a_file(tmp_path, monkeypatch, isolated_process_state):
    env = tmp_path / ".env"
    env.write_text(VALID_ENV)
    started = []

    class FakeApp:
        def __init__(self, controller):
            started.append(controller)

        def run(self):
            started.append("ran")

    monkeypatch.setattr(tray, "TrayApp", FakeApp)
    assert tray.main(["--env-file", str(env)]) == 0
    controller = started[0]
    assert started[1] == "ran"
    assert controller.env_file == env.resolve()
    assert Path.cwd() == tmp_path.resolve()
    assert "tray started" in (tmp_path / "data" / "keep-to-bring.log").read_text()
