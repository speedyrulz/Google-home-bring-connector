"""Adding items to a Bring! shopping list.

Uses the unofficial ``bring-api`` library (the same one Home Assistant's
official Bring! integration uses). Bring! has no public API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from bring_api import Bring
from bring_api.exceptions import BringAuthException, BringRequestException

from .catalog import Catalog

_LOGGER = logging.getLogger(__name__)


class BringAuthError(Exception):
    """Bring! rejected the email/password."""


class BringListNotFound(Exception):
    """The configured Bring! list name doesn't exist."""


class BringUnavailable(ConnectionError):
    """Bring! could not be reached (network problem or Bring! outage)."""


@dataclass(frozen=True)
class BringListInfo:
    uuid: str
    name: str


class BringSink:
    def __init__(
        self,
        email: str,
        password: str,
        list_name: str | None,
        session: aiohttp.ClientSession,
    ) -> None:
        self._bring = Bring(session, email, password)
        self._wanted_list = list_name
        self.email = email
        self.list: BringListInfo | None = None
        self.lists: list[BringListInfo] = []
        self.catalog: Catalog = Catalog.empty()

    @property
    def connected(self) -> bool:
        return self.list is not None

    @property
    def list_name(self) -> str:
        return self.list.name if self.list else "?"

    async def _login(self) -> None:
        try:
            await self._bring.login()
        except BringAuthException as err:
            raise BringAuthError(
                f"Bring! rejected the login for {self.email}. Check BRING_EMAIL and "
                "BRING_PASSWORD. If you normally sign in to Bring! with Google or Apple, "
                "set a password in the Bring! app first (Profile > Settings > Change password)."
            ) from err
        except BringRequestException as err:
            raise BringUnavailable(
                "Could not reach Bring! - is the internet connection working? "
                f"({err.__cause__ or err})"
            ) from err

    async def connect(self) -> None:
        """Log in, pick the target list and load the matching item catalogue."""
        await self._login()
        response = await self._bring.load_lists()
        self.lists = [BringListInfo(item.listUuid, item.name) for item in response.lists]
        if not self.lists:
            raise BringListNotFound(f"The Bring! account {self.email} has no shopping lists.")
        self.list = await self._choose_list()
        list_settings = self._bring.user_list_settings.get(self.list.uuid, {})
        locale = list_settings.get("listArticleLanguage") or self._bring.user_locale
        self.catalog = Catalog.for_locale(locale)
        _LOGGER.info(
            "Connected to Bring! as %s; adding items to list %r (catalogue %s, %d items)",
            self.email,
            self.list.name,
            locale,
            len(self.catalog),
        )

    async def _choose_list(self) -> BringListInfo:
        if self._wanted_list:
            wanted = self._wanted_list.strip().casefold()
            for info in self.lists:
                if info.name.strip().casefold() == wanted:
                    return info
            names = ", ".join(repr(info.name) for info in self.lists)
            raise BringListNotFound(
                f"No Bring! list called {self._wanted_list!r}. Your lists are: {names}. "
                "Fix BRING_LIST_NAME (or remove it to use your default list)."
            )
        if len(self.lists) > 1:
            default_uuid = await self._default_list_uuid()
            for info in self.lists:
                if info.uuid == default_uuid:
                    return info
        return self.lists[0]

    async def _default_list_uuid(self) -> str | None:
        try:
            settings = await self._bring.get_all_user_settings()
        except Exception:  # noqa: BLE001 - optional nicety, never fatal
            _LOGGER.debug("Could not read Bring! default list", exc_info=True)
            return None
        for entry in settings.usersettings:
            if entry.key == "defaultListUUID":
                return entry.value
        return None

    async def _call(self, func, *args):
        """Call the API, logging in again once if the session has expired."""
        try:
            return await func(*args)
        except BringAuthException:
            _LOGGER.info("Bring! session expired; logging in again")
            await self._login()
            return await func(*args)

    async def current_items(self) -> dict[str, str]:
        """Items currently on the list (to buy): ``{casefolded name: specification}``."""
        assert self.list is not None, "connect() first"
        response = await self._call(self._bring.get_list, self.list.uuid)
        return {item.itemId.casefold(): item.specification for item in response.items.purchase}

    async def add(self, name: str, spec: str = "") -> None:
        """Add an item (or update its specification if it's already on the list)."""
        assert self.list is not None, "connect() first"
        await self._call(self._bring.save_item, self.list.uuid, name, spec)

    async def remove(self, name: str) -> None:
        assert self.list is not None, "connect() first"
        await self._call(self._bring.remove_item, self.list.uuid, name)
