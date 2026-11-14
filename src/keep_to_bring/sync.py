"""The sync engine: move new Google Keep shopping-list items into Bring!.

Delivery is *at least once*: an item is only removed from Keep after Bring!
confirmed it was added. If anything fails midway the item simply stays in
Keep and is retried on the next cycle; because items already on the Bring!
list are skipped, a retry never creates duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .bring_sink import BringAuthError, BringListNotFound, BringSink
from .config import GoogleAccount
from .keep_source import KeepAuthError, KeepEntry, KeepSource
from .parsing import ParsedItem, parse_quantity, split_entries

_LOGGER = logging.getLogger(__name__)

# Shown on the Bring! list when a Google login stops working, so the problem
# is noticed by whoever opens the shopping list, not only by someone reading
# logs. Removed again automatically once the login works.
ALERT_ITEM_NAME = "⚠ Voice list: fix Google login"
MAX_BACKOFF_SECONDS = 1800.0


def merge_spec(current: str, new: str) -> str:
    """Combine an existing Bring! specification with a newly spoken one."""
    current, new = current.strip(), new.strip()
    if not new:
        return current
    if not current:
        return new
    if new.casefold() in current.casefold():
        return current
    return f"{current}, {new}"


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    already_on_list: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    removed_from_keep: int = 0
    accounts_ok: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    auth_failures: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.failed and not self.source_errors

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.already_on_list or self.removed_from_keep)

    def log(self) -> None:
        if self.added:
            _LOGGER.info("Added to Bring!: %s", ", ".join(self.added))
        if self.updated:
            _LOGGER.info("Updated on Bring!: %s", ", ".join(self.updated))
        if self.already_on_list:
            _LOGGER.info("Already on Bring!: %s", ", ".join(self.already_on_list))
        if self.removed_from_keep:
            _LOGGER.info("Cleared %d item(s) from the Google Keep list", self.removed_from_keep)
        if self.failed:
            _LOGGER.warning("Could not add (will retry): %s", ", ".join(self.failed))
        if not self.changed and not self.failed:
            _LOGGER.debug("No new items")


class Connector:
    def __init__(
        self,
        sources: Sequence[KeepSource],
        sink: BringSink,
        *,
        parse_quantities: bool = True,
        split_items: bool = False,
        alert_in_bring: bool = True,
        reload_accounts: Callable[[], Mapping[str, GoogleAccount]] | None = None,
    ) -> None:
        self.sources = list(sources)
        self.sink = sink
        self.parse_quantities = parse_quantities
        self.split_items = split_items
        self.alert_in_bring = alert_in_bring
        # Re-reads the settings so a token fixed with `get-token` is picked up
        # without restarting. Returns {casefolded email: account}.
        self._reload_accounts = reload_accounts
        # None = unknown (e.g. an alert left over from before a restart).
        self._alert_active: bool | None = None if alert_in_bring else False
        self._alert_spec = ""

    # ------------------------------------------------------------------
    # Text -> Bring! items
    # ------------------------------------------------------------------

    def _parse(self, text: str) -> ParsedItem:
        return parse_quantity(text) if self.parse_quantities else ParsedItem(text)

    def itemize(self, text: str) -> list[ParsedItem]:
        """Turn one Keep item's text into Bring! items (catalogue names, specs)."""
        catalog = self.sink.catalog

        def is_known(phrase: str) -> bool:
            return catalog.match(self._parse(phrase).name) is not None

        items = []
        for piece in split_entries(text, split_items=self.split_items, is_known=is_known):
            parsed = self._parse(piece)
            items.append(ParsedItem(catalog.display_name(parsed.name), parsed.spec))
        return items

    # ------------------------------------------------------------------
    # One sync cycle
    # ------------------------------------------------------------------

    async def ensure_connected(self) -> None:
        if not self.sink.connected:
            await self.sink.connect()

    def _refresh_broken_logins(self) -> None:
        broken = [s for s in self.sources if s.auth_broken]
        if not broken or self._reload_accounts is None:
            return
        try:
            accounts = self._reload_accounts()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not re-read settings: %s", err)
            return
        for source in broken:
            fresh = accounts.get(source.email.casefold())
            if fresh is not None and fresh.master_token != source.account.master_token:
                _LOGGER.info("Found a new Google login for %s; trying it now", source.email)
                source.update_account(fresh)

    async def pull_all(self, report: SyncReport) -> list[tuple[KeepSource, list[KeepEntry]]]:
        self._refresh_broken_logins()
        pulled: list[tuple[KeepSource, list[KeepEntry]]] = []
        for source in self.sources:
            if source.waiting_for_retry():
                report.source_errors[source.email] = "Google login failed; waiting before retrying"
                report.auth_failures.append(source.email)
                continue
            try:
                entries = await asyncio.to_thread(source.pull)
            except KeepAuthError as err:
                _LOGGER.error("%s", err)
                report.source_errors[source.email] = str(err)
                report.auth_failures.append(source.email)
                continue
            except Exception as err:  # noqa: BLE001 - network hiccups etc.; retried next cycle
                _LOGGER.warning(
                    "Could not read Google Keep for %s (%s: %s); will retry.",
                    source.email,
                    type(err).__name__,
                    err,
                )
                source.reset()
                report.source_errors[source.email] = f"{type(err).__name__}: {err}"
                continue
            report.accounts_ok.append(source.email)
            if entries:
                pulled.append((source, entries))
        return pulled

    async def sync_once(self) -> SyncReport:
        """Run one cycle. Raises if Bring! is unreachable (nothing is removed from Keep then)."""
        await self.ensure_connected()
        report = SyncReport()
        pulled = await self.pull_all(report)

        all_accounts_ok = len(report.accounts_ok) == len(self.sources)
        need_alert_cleanup = (
            self.alert_in_bring and all_accounts_ok and self._alert_active is not False
        )
        if not pulled and not need_alert_cleanup and not report.auth_failures:
            return report

        existing = await self.sink.current_items() if (pulled or need_alert_cleanup) else {}
        await self._update_alert(report, existing, all_accounts_ok)

        for source, entries in pulled:
            done = []
            for entry in entries:
                if await self._transfer(entry, existing, report):
                    done.append(entry)
            if not done:
                continue
            source.mark_done(done)
            try:
                await asyncio.to_thread(source.push)
            except Exception as err:  # noqa: BLE001
                # The source has dropped its local state; next cycle re-reads
                # Keep, finds these items still there, sees they are already
                # on Bring! (so no duplicates) and clears them from Keep.
                _LOGGER.warning(
                    "Items were added to Bring! but clearing them from Google Keep (%s) "
                    "failed (%s: %s); will retry.",
                    source.email,
                    type(err).__name__,
                    err,
                )
                if isinstance(err, KeepAuthError):
                    report.auth_failures.append(source.email)
                report.source_errors[source.email] = f"{type(err).__name__}: {err}"
            else:
                report.removed_from_keep += len(done)
        return report

    async def _transfer(self, entry: KeepEntry, existing: dict[str, str], report: SyncReport) -> bool:
        """Copy one Keep entry to Bring!. True if it can be removed from Keep."""
        items = self.itemize(entry.text)
        if not items:
            return False
        all_ok = True
        for item in items:
            key = item.name.casefold()
            spec = item.spec
            already = key in existing
            if already:
                spec = merge_spec(existing[key], item.spec)
                if spec == existing[key]:
                    report.already_on_list.append(item.label())
                    continue
            try:
                await self.sink.add(item.name, spec)
            except Exception as err:  # noqa: BLE001
                all_ok = False
                report.failed.append(item.label())
                _LOGGER.warning("Bring! refused %r: %s: %s", item.label(), type(err).__name__, err)
                continue
            existing[key] = spec
            (report.updated if already else report.added).append(
                ParsedItem(item.name, spec).label()
            )
        return all_ok

    async def _update_alert(
        self, report: SyncReport, existing: dict[str, str], all_accounts_ok: bool
    ) -> None:
        if not self.alert_in_bring:
            return
        try:
            if report.auth_failures:
                spec = "Run keep-to-bring get-token for " + ", ".join(sorted(set(report.auth_failures)))
                if self._alert_active and spec == self._alert_spec:
                    return
                await self.sink.add(ALERT_ITEM_NAME, spec)
                self._alert_active, self._alert_spec = True, spec
                _LOGGER.info("Put a reminder on the Bring! list to fix the Google login")
            elif all_accounts_ok and self._alert_active is not False:
                if ALERT_ITEM_NAME.casefold() in existing:
                    await self.sink.remove(ALERT_ITEM_NAME)
                    existing.pop(ALERT_ITEM_NAME.casefold(), None)
                    _LOGGER.info("Google login works again; removed the reminder from Bring!")
                self._alert_active, self._alert_spec = False, ""
        except Exception as err:  # noqa: BLE001 - the alert is best effort
            _LOGGER.debug("Could not update the Bring! reminder: %s", err)


# ----------------------------------------------------------------------
# Long-running loop
# ----------------------------------------------------------------------


def write_heartbeat(path: Path, report: SyncReport) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{datetime.now(timezone.utc).isoformat()} accounts={len(report.accounts_ok)}\n",
            encoding="utf-8",
        )
    except OSError as err:
        _LOGGER.debug("Could not write heartbeat %s: %s", path, err)


async def run_forever(
    connector: Connector,
    *,
    interval: float,
    heartbeat_file: Path | None,
    stop: asyncio.Event,
) -> None:
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            report = await connector.sync_once()
        except (BringAuthError, BringListNotFound) as err:
            failures += 1
            _LOGGER.error("%s", err)
        except Exception as err:  # noqa: BLE001
            failures += 1
            _LOGGER.warning("Sync failed (%s: %s); will retry.", type(err).__name__, err)
            _LOGGER.debug("Sync failure details", exc_info=True)
        else:
            failures = 0
            report.log()
            if report.healthy and heartbeat_file is not None:
                write_heartbeat(heartbeat_file, report)

        delay = interval if failures == 0 else min(interval * 2**failures, MAX_BACKOFF_SECONDS)
        delay = max(0.0, delay - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
