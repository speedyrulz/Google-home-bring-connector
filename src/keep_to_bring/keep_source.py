"""Reading (and clearing) the Google Keep shopping list.

Uses the unofficial ``gkeepapi`` library: Google's official Keep API is only
available to Google Workspace business accounts, not personal Gmail accounts.

All methods here are blocking (``gkeepapi`` uses ``requests``); the sync
engine runs them in a worker thread. Every request gets a timeout (see
``net.py``) because gkeepapi itself never sets one.
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
from .net import install_request_timeouts

_LOGGER = logging.getLogger(__name__)

AUTH_RETRY_BASE_SECONDS = 300.0
AUTH_RETRY_MAX_SECONDS = 3600.0

# gpsoauth "Error" values that really mean the saved login is no good.
# Anything else (an outage page, "ServiceUnavailable", no answer at all) is
# treated as temporary, so a Google hiccup doesn't raise a false alarm.
CREDENTIAL_ERRORS = frozenset(
    {
        "BadAuthentication",
        "NeedsBrowser",
        "NotVerified",
        "TermsNotAgreed",
        "CaptchaRequired",
        "AccountDeleted",
        "AccountDisabled",
        "ServiceDisabled",
        "DeviceManagementRequiredOrSyncDisabled",
    }
)


class KeepAuthError(Exception):
    """Google rejected the master token (revoked, expired or wrong)."""


class KeepTemporaryError(Exception):
    """Google answered unexpectedly; retry later (the login is probably fine)."""


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
        install_request_timeouts()

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
        except (keep_exc.LoginException, keep_exc.APIException) as err:
            translated = self._classify(err)
            if translated is err:
                raise
            raise translated from err
        self._keep = keep
        self._auth_failures = 0
        self.retry_at = 0.0
        _LOGGER.info("Connected to Google Keep as %s", self.email)

    def _sync(self) -> bool:
        """Sync with Google. True if Google forced a full resync (local edits discarded)."""
        resynced = False
        try:
            try:
                self._keep.sync()
            except keep_exc.ResyncRequiredException:
                _LOGGER.info("Google Keep asked for a full resync (%s)", self.email)
                self._keep.sync(resync=True)
                resynced = True
        except (keep_exc.LoginException, keep_exc.APIException) as err:
            self.reset()
            translated = self._classify(err)
            if translated is err:
                raise
            raise translated from err
        except Exception:
            # gkeepapi clears its "dirty" flags *before* sending, so after a
            # failed request local edits (our deletions) are silently dropped
            # while the local copy still shows them as done. Start over from
            # the server's state next time so nothing gets stuck.
            self.reset()
            raise
        self._auth_failures = 0
        self.retry_at = 0.0
        return resynced

    def _classify(self, err: Exception) -> Exception:
        """Turn a gkeepapi error into KeepAuthError (bad login) or a temporary error."""
        if isinstance(err, keep_exc.BrowserLoginRequiredException):
            self._auth_failed()
            return KeepAuthError(_auth_message(self.email, "NeedsBrowser"))
        if isinstance(err, keep_exc.LoginException):
            code = str(err.args[0]) if err.args and err.args[0] else ""
            if code in CREDENTIAL_ERRORS:
                self._auth_failed()
                return KeepAuthError(_auth_message(self.email, code))
            return KeepTemporaryError(
                f"Google's login service gave an unexpected answer for {self.email} "
                f"({code or 'no details'}); will retry."
            )
        if isinstance(err, keep_exc.APIException) and err.code == 401:
            self._auth_failed()
            return KeepAuthError(_auth_message(self.email, "HTTP 401"))
        return err

    def _auth_failed(self) -> None:
        self._auth_failures += 1
        exponent = min(self._auth_failures - 1, 10)  # avoid huge numbers after weeks of failures
        delay = min(AUTH_RETRY_BASE_SECONDS * 2**exponent, AUTH_RETRY_MAX_SECONDS)
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
        if self._keep is not None and self._sync():
            # A forced resync throws away local edits, so we can't be sure the
            # items were cleared. Say so; the next cycle re-checks them.
            raise KeepTemporaryError(
                "Google Keep asked for a full resync before the copied items could be "
                "cleared; will retry."
            )


def _auth_message(email: str, detail: str) -> str:
    return (
        f"Google refused the saved login for {email} ({detail}). The master token was "
        "probably revoked (this happens after a password change, a security check, or "
        "occasionally at random). Create a new one with `keep-to-bring get-token`."
    )
