"""The background service the tray icon uses: real libraries, fake servers."""

import asyncio
import threading
import time

import pytest

from keep_to_bring.bring_sink import BringAuthError, BringUnavailable
from keep_to_bring.config import load_settings
from keep_to_bring.service import ERROR, OK, STOPPED, WARNING, ConnectorService, status_from_cycle
from keep_to_bring.sync import SyncReport, run_forever


@pytest.fixture
def settings(keep_server, bring_server, google_account, tmp_path):
    return load_settings(
        {
            "GOOGLE_EMAIL": google_account.email,
            "GOOGLE_MASTER_TOKEN": google_account.master_token,
            "BRING_EMAIL": bring_server.email,
            "BRING_PASSWORD": bring_server.password,
            "POLL_INTERVAL_SECONDS": "3600",  # only "sync now" triggers later cycles
            "DATA_DIR": str(tmp_path / "data"),
        }
    )


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_service_syncs_reports_status_and_stops(settings, keep_server, bring_server):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    updates = []
    service = ConnectorService(settings, on_update=updates.append)
    service.start()
    try:
        wait_for(lambda: service.status.state == OK)
        status = service.status
        assert status.recent_items == ("Milk",)
        assert status.new_items == ("Milk",)
        assert status.items_added == 1
        assert status.last_check is not None
        assert bring_server.purchase["list-home"] == {"Milch": ""}
        assert keep_server.live_texts(list_id) == []
        assert (settings.data_dir / "heartbeat").exists()

        # "Sync now" runs a cycle right away instead of in an hour.
        keep_server.add_item(list_id, "eggs")
        service.sync_now()
        wait_for(lambda: service.status.items_added == 2)
        assert service.status.recent_items == ("Milk", "Eggs")
        assert service.status.new_items == ("Eggs",)
    finally:
        started = time.monotonic()
        assert service.stop()
        assert time.monotonic() - started < 5
    assert service.status.state == STOPPED
    assert not service.running
    assert updates[-1].state == STOPPED


def test_wrong_bring_password_is_an_error(settings, bring_server):
    bring_server.password = "changed"
    service = ConnectorService(settings)
    service.start()
    try:
        wait_for(lambda: service.status.state == ERROR)
        assert "Bring! rejected the login" in service.status.message
    finally:
        service.stop()
    assert service.status.state == ERROR  # stays visible after stopping


def test_stop_right_after_start(settings):
    service = ConnectorService(settings)
    service.start()
    assert service.stop()


def test_stop_before_start_is_harmless(settings):
    assert ConnectorService(settings).stop()


def test_cannot_start_twice(settings):
    service = ConnectorService(settings)
    service.start()
    try:
        with pytest.raises(RuntimeError):
            service.start()
    finally:
        service.stop()


@pytest.mark.parametrize(
    ("report", "error", "state", "fragment"),
    [
        (None, BringAuthError("Bring! rejected the login"), ERROR, "rejected"),
        (None, BringUnavailable("offline"), WARNING, "Can't reach Bring!"),
        (None, RuntimeError("boom"), WARNING, "RuntimeError"),
        (SyncReport(auth_failures=["me@example.com"]), None, ERROR, "me@example.com"),
        (SyncReport(source_errors={"me@example.com": "x"}), None, WARNING, "Google Keep"),
        (SyncReport(failed=["Milk"]), None, WARNING, "refused"),
        (SyncReport(added=["Milk"]), None, OK, "Working"),
    ],
)
def test_status_from_cycle(report, error, state, fragment):
    got_state, message = status_from_cycle(report, error)
    assert got_state == state
    assert fragment in message


class _Counting:
    def __init__(self, fail=False):
        self.sources = []
        self.calls = 0
        self.fail = fail

    async def sync_once(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return SyncReport()


async def test_run_forever_wake_and_on_cycle():
    connector = _Counting()
    stop, wake = asyncio.Event(), asyncio.Event()
    seen = []
    task = asyncio.create_task(
        run_forever(connector, interval=3600, heartbeat_file=None, stop=stop, wake=wake,
                    on_cycle=lambda report, error: seen.append((report, error)))
    )
    while connector.calls < 1:
        await asyncio.sleep(0.01)
    wake.set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if connector.calls >= 2:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert connector.calls == 2
    assert all(isinstance(report, SyncReport) and error is None for report, error in seen)


async def test_run_forever_reports_errors_and_survives_a_broken_callback():
    connector = _Counting(fail=True)
    stop = asyncio.Event()
    seen = []

    def callback(report, error):
        seen.append(error)
        stop.set()
        raise ValueError("listener bug")

    await asyncio.wait_for(
        run_forever(connector, interval=0.01, heartbeat_file=None, stop=stop, on_cycle=callback), 2
    )
    assert isinstance(seen[0], RuntimeError)


def test_status_listener_errors_do_not_kill_the_service(settings, keep_server):
    keep_server.add_list("Shopping list", [])
    calls = threading.Event()

    def bad_listener(status):
        calls.set()
        raise ValueError("listener bug")

    service = ConnectorService(settings, on_update=bad_listener)
    service.start()
    try:
        wait_for(lambda: service.status.state == OK)
        assert calls.is_set()
        assert service.running
    finally:
        service.stop()
