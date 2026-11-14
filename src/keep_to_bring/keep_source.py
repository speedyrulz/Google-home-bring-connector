"""Reading (and clearing) the Google Keep shopping list.

Uses the unofficial ``gkeepapi`` library: Google's official Keep API is only
available to Google Workspace business accounts, not personal Gmail accounts.

All methods here are blocking (``gkeepapi`` uses ``requests``); the sync
engine runs them in a worker thread.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import gkeepapi
from gkeepapi import exception as keep_exc
from gkeepapi import node as keep_node

from .config import GoogleAccount

_LOGGER = logging.getLogger(__name__)

AUTH_RETRY_BASE_SECONDS = 300.0
AUTH_RETRY_MAX_SECONDS = 3600.0


class KeepAuthError(Exception):
    """Google rejected the master token (revoked, expired or wrong)."""


@dataclass
class KeepEntry:
    """One unchecked item on a Keep shopping list."""

    account: str
    list_title: str
    text: str
    item: Any = field(default=None, repr=False, compare=False)


class KeepSource:
    """One Google account's Keep shopping list(s)."""

    def __init__(
        self,
        account: GoogleAccount,
        list_titles: Sequence[str],
        action: str = "delete",
        keep_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if action not in {"delete", "check"}:
            raise ValueError(f"unknown action {action!r}")
        self.account = account
        self.list_titles = tuple(list_titles)
        self.action = action
        self._wanted = {title.strip().casefold() for title in list_titles}
        self._keep_factory = keep_factory
        self._clock = clock
        self._keep: Any = None
        self._warned_missing = False
        self._auth_failures = 0
        self.retry_at = 0.0

    @property
    def email(self) -> str:
        return self.account.email

    @property
    def connected(self) -> bool:
        return self._keep is not None

    @property
    def auth_broken(self) -> bool:
        """True after Google rejected the login, until a sync succeeds again."""
        return self._auth_failures > 0

    def waiting_for_retry(self) -> bool:
        """True while backing off after an authentication failure."""
        return self._clock() < self.retry_at

    def update_account(self, account: GoogleAccount) -> None:
        """Switch to new credentials (e.g. a fresh master token) and retry right away."""
        self.account = account
        self._keep = None
        self._auth_failures = 0
        self.retry_at = 0.0

    def reset(self) -> None:
        """Forget the local Keep state; the next pull reconnects with a full sync."""
        self._keep = None

    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Authenticate with the master token and do a full sync."""
        keep = self._keep_factory() if self._keep_factory else gkeepapi.Keep()
        try:
            keep.authenticate(
                self.account.email,
                self.account.master_token,
                device_id=self.account.device_id,
            )
        except keep_exc.LoginException as err:
            self._auth_failed()
            raise KeepAuthError(_auth_message(self.email, err)) from err
        except keep_exc.APIException as err:
            if err.code == 401:
                self._auth_failed()
                raise KeepAuthError(_auth_message(self.email, err)) from err
            raise
        self._keep = keep
        self._auth_failures = 0
        self.retry_at = 0.0
        _LOGGER.info("Connected to Google Keep as %s", self.email)

    def _sync(self) -> None:
        try:
            try:
                self._keep.sync()
            except keep_exc.ResyncRequiredException:
                _LOGGER.info("Google Keep asked for a full resync (%s)", self.email)
                self._keep.sync(resync=True)
        except keep_exc.LoginException as err:
            self._auth_failed()
            self.reset()
            raise KeepAuthError(_auth_message(self.email, err)) from err
        except keep_exc.APIException as err:
            self.reset()
            if err.code == 401:
                self._auth_failed()
                raise KeepAuthError(_auth_message(self.email, err)) from err
            raise
        except Exception:
            # gkeepapi clears its "dirty" flags *before* sending, so after a
            # failed request local edits (our deletions) are silently dropped
            # while the local copy still shows them as done. Start over from
            # the server's state next time so nothing gets stuck.
            self.reset()
            raise
        self._auth_failures = 0
        self.retry_at = 0.0

    def _auth_failed(self) -> None:
        self._auth_failures += 1
        delay = min(AUTH_RETRY_BASE_SECONDS * 2 ** (self._auth_failures - 1), AUTH_RETRY_MAX_SECONDS)
        self.retry_at = self._clock() + delay

    # ------------------------------------------------------------------

    def matching_lists(self) -> list[Any]:
        """The Keep checklists whose title matches one of the configured titles."""
        if self._keep is None:
            return []
        found = []
        for note in self._keep.all():
            if note.trashed or note.deleted:
                continue
            if (note.title or "").strip().casefold() not in self._wanted:
                continue
            if isinstance(note, keep_node.List):
                found.append(note)
            else:
                _LOGGER.warning(
                    "Keep note %r in %s is a plain note, not a checklist; skipping it.",
                    note.title,
                    self.email,
                )
        if found:
            self._warned_missing = False
        elif not self._warned_missing:
            self._warned_missing = True
            _LOGGER.warning(
                "No Google Keep checklist titled %s found in %s. Say \"Hey Google, add test "
                "to my shopping list\" once to create it, or set KEEP_LIST_TITLES to the "
                "name of your list.",
                " or ".join(repr(t) for t in self.list_titles),
                self.email,
            )
        return found

    def pull(self) -> list[KeepEntry]:
        """Sync with Google and return all unchecked items on the shopping list(s)."""
        if self._keep is None:
            self.connect()
        else:
            self._sync()
        entries: list[KeepEntry] = []
        for note in self.matching_lists():
            for item in note.unchecked:
                text = (item.text or "").strip()
                if text:
                    entries.append(KeepEntry(self.email, note.title, text, item))
        return entries

    def mark_done(self, entries: Iterable[KeepEntry]) -> int:
        """Delete (or tick) entries locally. Call :meth:`push` to send the change to Google."""
        count = 0
        for entry in entries:
            if self.action == "delete":
                entry.item.delete()
            else:
                entry.item.checked = True
            count += 1
        return count

    def push(self) -> None:
        """Send local changes (deleted/ticked items) to Google."""
        if self._keep is not None:
            self._sync()


def _auth_message(email: str, err: Exception) -> str:
    detail = str(err) or type(err).__name__
    return (
        f"Google refused the saved login for {email} ({detail}). The master token was "
        "probably revoked (this happens after a password change, a security check, or "
        "occasionally at random). Create a new one with `keep-to-bring get-token`."
    )
