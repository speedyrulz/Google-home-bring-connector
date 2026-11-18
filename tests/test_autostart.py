import subprocess
import sys

import pytest

from keep_to_bring import autostart
from keep_to_bring.autostart import APP_ID, RUN_KEY, WindowsAutostart, XdgAutostart


class FakeKey:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Just enough of the winreg module, backed by a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.keys: dict[str, dict[str, object]] = {}

    def OpenKey(self, root, path, reserved=0, access=1):  # noqa: N802 - winreg's names
        full = f"{root}\\{path}"
        if full not in self.keys:
            raise FileNotFoundError(full)
        return FakeKey(self.keys[full], full)

    def CreateKeyEx(self, root, path, reserved=0, access=2):  # noqa: N802
        full = f"{root}\\{path}"
        self.keys.setdefault(full, {})
        return FakeKey(self.keys[full], full)

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        assert kind == self.REG_SZ
        key.store[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


ARGS = [r"C:\Users\Jo Smith\bring\.venv\Scripts\keep-to-bring-tray.exe", "--env-file", r"C:\Users\Jo Smith\bring\.env"]


def test_windows_run_key_round_trip():
    reg = FakeWinreg()
    auto = WindowsAutostart(reg)
    assert auto.registered_command() is None
    auto.enable(ARGS)
    expected = subprocess.list2cmdline(ARGS)
    assert auto.registered_command() == expected == auto.command_for(ARGS)
    assert expected.startswith('"C:\\Users\\Jo Smith')  # paths with spaces are quoted
    assert reg.keys[f"HKCU\\{RUN_KEY}"] == {APP_ID: expected}
    auto.disable()
    assert auto.registered_command() is None
    auto.disable()  # already gone: no error


def test_windows_disable_without_key_is_fine():
    WindowsAutostart(FakeWinreg()).disable()


def test_xdg_autostart_round_trip(tmp_path):
    auto = XdgAutostart(config_home=tmp_path)
    assert auto.registered_command() is None
    args = ["/home/jo/my apps/keep-to-bring-tray", "--env-file", '/home/jo/"odd"/.env']
    auto.enable(args)
    text = auto.path.read_text()
    assert text.startswith("[Desktop Entry]\n")
    assert auto.path == tmp_path / "autostart" / "keep-to-bring.desktop"
    command = auto.registered_command()
    assert command == '"/home/jo/my apps/keep-to-bring-tray" --env-file "/home/jo/\\"odd\\"/.env"'
    assert command == auto.command_for(args)
    auto.disable()
    assert not auto.path.exists()
    auto.disable()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("linux", XdgAutostart), ("darwin", type(None))],
)
def test_for_this_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert isinstance(autostart.for_this_platform(), expected)


def test_for_this_platform_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg())
    assert isinstance(autostart.for_this_platform(), WindowsAutostart)
