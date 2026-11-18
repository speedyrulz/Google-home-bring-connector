"""Checks that need a real Windows machine (run by CI on windows-latest).

They use the real registry, PowerShell, the pip-generated launcher and the
real system tray, which the cross-platform tests can only imitate.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

FAKE_ENV = (
    "GOOGLE_EMAIL=me@example.com\n"
    "GOOGLE_MASTER_TOKEN=aas_et/not-a-real-token\n"
    "BRING_EMAIL=nobody@example.invalid\n"
    "BRING_PASSWORD=not-a-real-password\n"
    "POLL_INTERVAL_SECONDS=15\n"
)


def test_real_registry_autostart(monkeypatch):
    from keep_to_bring import autostart

    monkeypatch.setattr(autostart, "APP_ID", "KeepToBringTest")  # don't touch a real entry
    auto = autostart.WindowsAutostart()
    auto.disable()
    try:
        auto.enable([r"C:\Program Files\x\keep-to-bring-tray.exe", "--env-file", r"C:\a b\.env"])
        assert auto.registered_command() == (
            '"C:\\Program Files\\x\\keep-to-bring-tray.exe" --env-file "C:\\a b\\.env"'
        )
    finally:
        auto.disable()
    assert auto.registered_command() is None


def test_windowless_launcher_and_pythonw_are_found(tmp_path):
    from keep_to_bring.tray import launcher_command, python_executable

    command = launcher_command(tmp_path / ".env")
    assert command[0].lower().endswith("keep-to-bring-tray.exe"), command
    assert Path(command[0]).is_file()
    assert python_executable(windowed=True).lower().endswith("pythonw.exe")
    assert python_executable(windowed=False).lower().endswith("python.exe")


def test_start_menu_shortcut_is_created(tmp_path, monkeypatch):
    from keep_to_bring.tray import SHORTCUT_NAME, Desktop, make_icon_image

    monkeypatch.setenv("APPDATA", str(tmp_path))
    programs = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True)
    icon = tmp_path / "k.ico"
    make_icon_image("ok", 256).save(icon, sizes=[(16, 16), (32, 32), (256, 256)])
    Desktop().create_start_menu_shortcut([sys.executable, "-c", "pass"], icon, tmp_path)
    assert (programs / SHORTCUT_NAME).stat().st_size > 0


def test_real_tray_app_runs(tmp_path):
    from keep_to_bring.tray import python_executable

    env = tmp_path / ".env"
    env.write_text(FAKE_ENV)
    data = tmp_path / "data"
    data.mkdir()
    # Don't add the CI machine's startup entry; skip the welcome balloon.
    (data / "tray-settings.json").write_text('{"autostart_opt_out": true, "first_run_done": true}')
    appdata = tmp_path / "appdata"
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True)
    child_env = {k: v for k, v in os.environ.items() if k != "PYSTRAY_BACKEND"}  # use the real backend
    child_env["APPDATA"] = str(appdata)

    process = subprocess.Popen(
        [python_executable(windowed=True), "-m", "keep_to_bring.tray", "--env-file", str(env)],
        env=child_env,
        cwd=tmp_path,
    )
    log = data / "keep-to-bring.log"
    text = ""
    try:
        deadline = time.monotonic() + 90
        while "Connector started" not in text:
            assert process.poll() is None, f"tray app exited early:\n{text}"
            assert time.monotonic() < deadline, f"tray app did not start the connector:\n{text}"
            time.sleep(0.5)
            text = log.read_text(encoding="utf-8") if log.exists() else ""
        time.sleep(5)
        assert process.poll() is None, "tray app stopped running"
    finally:
        process.terminate()
        process.wait(15)
    text = log.read_text(encoding="utf-8")
    assert "Traceback" not in text and "CRITICAL" not in text, text
    assert (programs / "Keep to Bring.lnk").exists()
    assert (data / "keep-to-bring.ico").exists()
