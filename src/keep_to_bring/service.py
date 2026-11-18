"""Running the connector: shared builders, plus a background-thread service.

``ConnectorService`` runs the sync loop on its own thread and event loop so a
GUI (the system tray icon) can stay responsive, show the current status and
offer "sync now" / "quit".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import aiohttp

from .bring_sink import BringAuthError, BringListNotFound, BringSink, BringUnavailable
from .config import ConfigError, GoogleAccount, Settings, load_environment, load_settings
from .keep_source import KeepSource
from .sync import Connector, SyncReport, run_forever

_LOGGER = logging.getLogger(__name__)

NOISY_LOGGERS = ("gkeepapi", "urllib3", "bring_api", "aiohttp")


def quiet_noisy_loggers(level: str) -> None:
    if level != "DEBUG":
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=60))


def account_reloader(env_file: Path) -> Callable[[], dict[str, GoogleAccount]]:
    """Re-read the settings file so a token renewed with `get-token` is used without a restart."""

    def reload() -> dict[str, GoogleAccount]:
        try:
            settings = load_settings(load_environment(env_file))
        except ConfigError:
            return {}
        return {account.email.casefold(): account for account in settings.google_accounts}

    return reload


def build_connector(
    settings: Settings,
    sink: BringSink,
    reload_accounts: Callable[[], Mapping[str, GoogleAccount]] | None = None,
) -> Connector:
    sources = [
        KeepSource(account, settings.keep_list_titles, settings.keep_action)
        for account in settings.google_accounts
    ]
    return Connector(
        sources,
        sink,
        parse_quantities=settings.parse_quantities,
        split_items=settings.split_items,
        alert_in_bring=settings.alert_in_bring,
        reload_accounts=reload_accounts,
    )


# ----------------------------------------------------------------------
# Status shown by the tray icon
# ----------------------------------------------------------------------

STARTING, OK, WARNING, ERROR, STOPPED = "starting", "ok", "warning", "error", "stopped"


@dataclass(frozen=True)
class ServiceStatus:
    state: str = STARTING  # starting | ok | warning | error | stopped
    message: str = "Starting..."
    last_check: datetime | None = None
    recent_items: tuple[str, ...] = ()  # newest last
    items_added: int = 0  # since the service started
    new_items: tuple[str, ...] = field(default=(), compare=False)  # added in the latest cycle


def status_from_cycle(report: SyncReport | None, error: BaseException | None) -> tuple[str, str]:
    """(state, message) describing the outcome of one sync cycle."""
    if error is not None:
        if isinstance(error, (BringAuthError, BringListNotFound)):
            return ERROR, str(error)
        if isinstance(error, BringUnavailable):
            return WARNING, "Can't reach Bring! right now - will keep trying."
        return WARNING, f"Problem during sync ({type(error).__name__}) - will keep trying."
    assert report is not None
    if report.auth_failures:
        who = ", ".join(sorted(set(report.auth_failures)))
        return ERROR, f"Google login needs renewing for {who}."
    if report.source_errors:
        return WARNING, "Can't reach Google Keep right now - will keep trying."
    if report.failed:
        return WARNING, "Bring! refused some items - will retry."
    return OK, "Working"


class ConnectorService:
    """The sync loop on a background thread, with thread-safe controls."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_update: Callable[[ServiceStatus], None] | None = None,
        reload_accounts: Callable[[], Mapping[str, GoogleAccount]] | None = None,
    ) -> None:
        self.settings = settings
        self._on_update = on_update
        self._reload_accounts = reload_accounts
        self._status = ServiceStatus()
        self._status_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        self._ready = threading.Event()
        self._stop_requested = False

    @property
    def status(self) -> ServiceStatus:
        with self._status_lock:
            return self._status

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(target=self._thread_main, name="keep-to-bring-sync", daemon=True)
        self._thread.start()

    def sync_now(self) -> None:
        """Start a sync cycle right away (no-op if the loop isn't running)."""
        if self._ready.wait(5) and self._loop is not None and self._wake is not None:
            self._call_in_loop(self._wake.set)

    def stop(self, timeout: float = 15.0) -> bool:
        """Stop the loop and wait for the thread. True if it ended in time."""
        self._stop_requested = True
        if self._ready.wait(1) and self._loop is not None and self._stop is not None:
            self._call_in_loop(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    # ------------------------------------------------------------------

    def _call_in_loop(self, func: Callable[[], None]) -> None:
        try:
            self._loop.call_soon_threadsafe(func)  # type: ignore[union-attr]
        except RuntimeError:  # loop already closed
            pass

    def _publish(self, status: ServiceStatus) -> None:
        with self._status_lock:
            self._status = status
        if self._on_update is not None:
            try:
                self._on_update(status)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Status listener failed", exc_info=True)

    def _on_cycle(self, report: SyncReport | None, error: BaseException | None) -> None:
        state, message = status_from_cycle(report, error)
        previous = self.status
        new_items = tuple(report.added + report.updated) if report is not None else ()
        self._publish(
            replace(
                previous,
                state=state,
                message=message,
                last_check=datetime.now(),
                recent_items=(previous.recent_items + new_items)[-5:],
                items_added=previous.items_added + len(new_items),
                new_items=new_items,
            )
        )

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001 - never die silently
            _LOGGER.exception("The sync loop crashed")
            self._publish(replace(self.status, state=ERROR, message="Stopped after an unexpected error (see log)."))
        else:
            if self.status.state != ERROR:
                self._publish(replace(self.status, state=STOPPED, message="Stopped", new_items=()))
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        if self._stop_requested:
            self._stop.set()
        self._ready.set()
        settings = self.settings
        async with http_session() as session:
            sink = BringSink(settings.bring_email, settings.bring_password, settings.bring_list_name, session)
            connector = build_connector(settings, sink, self._reload_accounts)
            await run_forever(
                connector,
                interval=settings.poll_interval,
                heartbeat_file=settings.heartbeat_file,
                stop=self._stop,
                wake=self._wake,
                on_cycle=self._on_cycle,
            )
