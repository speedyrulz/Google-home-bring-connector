"""Regression tests for failure modes: stalled connections, Google hiccups, shutdown."""

import asyncio
import socket
import threading
import time

import aiohttp
import gkeepapi
import gpsoauth
import pytest
import requests

from keep_to_bring import net, sync
from keep_to_bring.bring_sink import BringSink
from keep_to_bring.keep_source import KeepAuthError, KeepSource, KeepTemporaryError
from keep_to_bring.sync import ALERT_ITEM_NAME, Connector, merge_spec, run_forever


@pytest.fixture
def stalled_server():
    """A TCP server that accepts connections but never answers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted = []
    stop = threading.Event()

    def accept_forever():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                accepted.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/"
    stop.set()
    thread.join(2)
    for conn in accepted:
        conn.close()
    listener.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def test_requests_get_a_default_timeout(stalled_server, monkeypatch):
    monkeypatch.setattr(net, "REQUEST_TIMEOUT", (1.0, 0.3))
    net.install_request_timeouts()
    net.install_request_timeouts()  # idempotent
    started = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        requests.Session().get(stalled_server)
    assert time.monotonic() - started < 3


def test_keep_pull_does_not_hang_on_a_stalled_connection(stalled_server, monkeypatch, google_account):
    monkeypatch.setattr(net, "REQUEST_TIMEOUT", (1.0, 0.3))
    monkeypatch.setattr(gpsoauth, "perform_oauth", lambda *a, **k: {"Auth": "x"})
    # A real Keep client with a real requests session, pointed at the stalled server.
    keep = gkeepapi.Keep()
    keep._keep_api._base_url = stalled_server
    keep._keep_api._session = requests.Session()
    source = KeepSource(google_account, ["Shopping list"], keep_factory=lambda: keep)
    started = time.monotonic()
    with pytest.raises(requests.exceptions.RequestException):
        source.pull()
    assert time.monotonic() - started < 3
    assert not source.auth_broken  # a network problem is not a login problem


@pytest.mark.parametrize("answer", [{}, {"Error": "ServiceUnavailable"}])
def test_google_hiccup_is_not_treated_as_revoked_login(keep_server, google_account, monkeypatch, answer):
    monkeypatch.setattr(gpsoauth, "perform_oauth", lambda *a, **k: answer)
    source = KeepSource(google_account, ["Shopping list"])
    with pytest.raises(KeepTemporaryError, match="will retry"):
        source.pull()
    assert not source.auth_broken
    assert not source.waiting_for_retry()


def test_bad_authentication_is_still_a_login_problem(keep_server, google_account, monkeypatch):
    monkeypatch.setattr(gpsoauth, "perform_oauth", lambda *a, **k: {"Error": "BadAuthentication"})
    source = KeepSource(google_account, ["Shopping list"])
    with pytest.raises(KeepAuthError, match="BadAuthentication"):
        source.pull()
    assert source.auth_broken


async def test_google_hiccup_raises_no_alert_on_bring(keep_server, bring_server, google_account, session, monkeypatch):
    monkeypatch.setattr(gpsoauth, "perform_oauth", lambda *a, **k: {})
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    connector = Connector([KeepSource(google_account, ["Shopping list"])], sink)
    report = await connector.sync_once()
    assert not report.healthy
    assert report.auth_failures == []
    assert ALERT_ITEM_NAME not in bring_server.purchase["list-home"]


async def test_forced_resync_during_clearing_is_not_reported_as_cleared(
    keep_server, bring_server, google_account, session
):
    list_id = keep_server.add_list("Shopping list", [])
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    connector = Connector([KeepSource(google_account, ["Shopping list"])], sink)
    await connector.sync_once()
    keep_server.add_item(list_id, "eggs")

    original, calls = keep_server.handle, {"n": 0}

    def resync_on_clear(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the request that would clear "eggs"
            keep_server.force_resync_next = True
        return original(**kwargs)

    keep_server.handle = resync_on_clear
    report = await connector.sync_once()
    assert report.added == ["Eggs"]
    assert report.removed_from_keep == 0
    assert not report.healthy
    assert keep_server.live_texts(list_id) == ["eggs"]

    report = await connector.sync_once()
    assert keep_server.live_texts(list_id) == []
    assert report.already_on_list == ["Eggs"]
    assert bring_server.purchase["list-home"] == {"Eier": ""}


async def test_existing_item_is_updated_under_its_exact_name(keep_server, bring_server, google_account, session):
    bring_server.purchase["list-home"] = {"oat milk": ""}  # custom item typed in lowercase
    keep_server.add_list("Shopping list", ["2 oat milk"])
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    connector = Connector([KeepSource(google_account, ["Shopping list"])], sink)
    await connector.sync_once()
    assert bring_server.purchase["list-home"] == {"oat milk": "2"}


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        ("12", "2", "12, 2"),  # "2" is not "already in" "12"
        ("2 lbs", "2", "2 lbs, 2"),
        ("x", "organic, large", "x, organic, large"),
        ("organic, large", "Large", "organic, large"),
    ],
)
def test_merge_spec_compares_whole_parts(current, new, expected):
    assert merge_spec(current, new) == expected
    assert merge_spec(expected, new) == expected  # retrying changes nothing


class _AlwaysFailing:
    def __init__(self):
        self.sources = []
        self.calls = 0

    async def sync_once(self):
        self.calls += 1
        raise RuntimeError("Bring! is down")


async def test_run_forever_survives_endless_failures(monkeypatch):
    monkeypatch.setattr(sync, "MAX_BACKOFF_SECONDS", 0.0)
    connector = _AlwaysFailing()
    stop = asyncio.Event()

    async def stop_later():
        while connector.calls < 1100:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(run_forever(connector, interval=0.001, heartbeat_file=None, stop=stop), stop_later()),
        timeout=30,
    )
    assert connector.calls >= 1100


class _StuckOnce(KeepSource):
    """A Keep source whose first pull hangs (like a dead connection)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stuck_once = False

    def pull(self):
        if not self.stuck_once:
            self.stuck_once = True
            self.entered.set()
            self.release.wait(30)
            return []
        return super().pull()


async def test_stop_is_prompt_even_while_keep_is_stuck(keep_server, bring_server, google_account, session):
    source = _StuckOnce(google_account, ["Shopping list"])
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(Connector([source], sink), interval=60, heartbeat_file=None, stop=stop))
    try:
        while not source.entered.is_set():
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2)
    finally:
        source.release.set()


async def test_stuck_cycle_is_abandoned_and_syncing_resumes(
    keep_server, bring_server, google_account, session, monkeypatch, caplog
):
    monkeypatch.setattr(sync, "CYCLE_TIMEOUT_SECONDS", 0.3)
    keep_server.add_list("Shopping list", ["milk"])
    source = _StuckOnce(google_account, ["Shopping list"])
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(Connector([source], sink), interval=0.05, heartbeat_file=None, stop=stop))
    try:
        for _ in range(300):
            await asyncio.sleep(0.02)
            if "Milch" in bring_server.purchase["list-home"]:
                break
        stop.set()
        await asyncio.wait_for(task, 5)
    finally:
        source.release.set()
    assert bring_server.purchase["list-home"] == {"Milch": ""}
    assert any("abandoned" in record.message for record in caplog.records)
