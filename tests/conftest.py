"""Test doubles: an in-memory Google Keep server and Bring! server.

The *real* gkeepapi and bring-api libraries are used in the tests; only the
HTTP layer underneath them is replaced, so the tests exercise the actual
request/response handling of both libraries.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from typing import Any

import bring_api.bring
import gkeepapi
import gpsoauth
import pytest
from aiohttp import web
from gkeepapi import node as keep_node

from keep_to_bring.config import GoogleAccount, default_device_id

# ----------------------------------------------------------------------
# Google Keep
# ----------------------------------------------------------------------


class FakeKeepResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self._payload


class FakeKeepServer:
    """Mimics the ``notes/v1/changes`` endpoint gkeepapi talks to."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.node_versions: dict[str, int] = {}
        self.version = 1
        self.requests: list[dict] = []
        self.valid_tokens: set[str] = set()
        self.fail_next: list[Exception] = []  # raised by the next request(s)
        self.force_resync_next = False

    # --- building server state -----------------------------------------

    def _store(self, raw: dict) -> None:
        raw = copy.deepcopy(raw)
        raw.setdefault("serverId", "srv." + raw["id"])
        self.version += 1
        self.nodes[raw["id"]] = raw
        self.node_versions[raw["id"]] = self.version

    def add_list(self, title: str, items: list[str | tuple[str, bool]], **attrs: Any) -> str:
        note = keep_node.List()
        note.title = title
        for entry in items:
            text, checked = (entry, False) if isinstance(entry, str) else entry
            note.add(text, checked)
        raw = note.save()
        raw.update(attrs)
        self._store(raw)
        for child in note.children:
            child_raw = child.save()
            child_raw["parentServerId"] = "srv." + note.id
            self._store(child_raw)
        return note.id

    def add_note(self, title: str, text: str) -> str:
        note = keep_node.Note()
        note.title = title
        note.text = text
        self._store(note.save())
        for child in note.children:
            self._store(child.save())
        return note.id

    def add_item(self, list_id: str, text: str) -> str:
        """Simulate Google Home adding an item to an existing list."""
        item = keep_node.ListItem(parent_id=list_id)
        item.text = text
        raw = item.save()
        raw["parentServerId"] = "srv." + list_id
        self._store(raw)
        return item.id

    def items(self, list_id: str) -> list[dict]:
        return [n for n in self.nodes.values() if n.get("parentId") == list_id]

    def live_texts(self, list_id: str) -> list[str]:
        return sorted(
            n["text"]
            for n in self.items(list_id)
            if not n.get("checked") and not _is_set(n.get("timestamps", {}).get("deleted"))
        )

    # --- HTTP ------------------------------------------------------------

    def handle(self, url: str, method: str, json: dict | None = None, headers: dict | None = None, **_: Any):
        if self.fail_next:
            raise self.fail_next.pop(0)
        assert url.endswith("notes/v1/changes"), url
        assert method == "POST"
        assert (headers or {}).get("Authorization", "").startswith("OAuth "), "not authenticated"
        payload = json or {}
        self.requests.append(copy.deepcopy(payload))
        if self.force_resync_next and payload.get("targetVersion"):
            self.force_resync_next = False
            return FakeKeepResponse({"forceFullResync": True, "toVersion": str(self.version), "truncated": False})
        for raw in payload.get("nodes", []):
            self._store(raw)
        since = int(payload.get("targetVersion") or 0)
        changed = [copy.deepcopy(self.nodes[nid]) for nid, ver in self.node_versions.items() if ver > since]
        return FakeKeepResponse(
            {"kind": "notes#downstream", "toVersion": str(self.version), "truncated": False, "nodes": changed}
        )


def _is_set(timestamp: str | None) -> bool:
    return bool(timestamp) and not timestamp.startswith("1970-01-01")


class FakeKeepSession:
    def __init__(self, server: FakeKeepServer) -> None:
        self.server = server

    def request(self, **kwargs: Any) -> FakeKeepResponse:
        return self.server.handle(**kwargs)


@pytest.fixture
def keep_server(monkeypatch: pytest.MonkeyPatch) -> FakeKeepServer:
    server = FakeKeepServer()

    def fake_perform_oauth(email, master_token, android_id, **kwargs):
        if master_token in server.valid_tokens:
            assert android_id == default_device_id(email)
            return {"Auth": f"auth-for-{email}"}
        return {"Error": "BadAuthentication"}

    monkeypatch.setattr(gpsoauth, "perform_oauth", fake_perform_oauth)

    original_init = gkeepapi.KeepAPI.__init__

    def patched_init(self, auth=None):
        original_init(self, auth)
        self._session = FakeKeepSession(server)

    monkeypatch.setattr(gkeepapi.KeepAPI, "__init__", patched_init)
    return server


@pytest.fixture
def google_account(keep_server: FakeKeepServer) -> GoogleAccount:
    email = "me@example.com"
    token = "aas_et/valid-token"
    keep_server.valid_tokens.add(token)
    return GoogleAccount(email, token, default_device_id(email))


# ----------------------------------------------------------------------
# Bring!
# ----------------------------------------------------------------------

class FakeBringServer:
    """A real local HTTP server speaking enough of the Bring! REST API.

    Runs on its own thread and event loop so it serves both async tests and
    synchronous CLI tests (which call ``asyncio.run`` themselves).
    """

    def __init__(self) -> None:
        self.email = "bring@example.com"
        self.password = "secret"
        self.user_uuid = "user-uuid"
        self.locale = "en-US"
        self.default_list = "list-home"
        self.lists = [
            {"listUuid": "list-home", "name": "Home", "theme": "ch.publisheria.bring.theme.home"},
            {"listUuid": "list-cabin", "name": "Cabin", "theme": "ch.publisheria.bring.theme.home"},
        ]
        # listUuid -> {itemId (catalogue key): spec}
        self.purchase: dict[str, dict[str, str]] = {"list-home": {}, "list-cabin": {}}
        self.put_requests: list[dict] = []
        self.login_count = 0
        self.fail_puts = 0  # number of upcoming PUTs to fail with HTTP 500
        self.fail_gets = 0  # number of upcoming list GETs to fail with HTTP 500
        self.reject_items: set[str] = set()  # catalogue keys whose PUT fails
        self.base_url = ""

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve() -> None:
            asyncio.set_event_loop(self._loop)
            app = web.Application()
            app.router.add_post("/rest/v2/bringauth", self._auth)
            app.router.add_get("/rest/v2/bringusers/{user}", self._account)
            app.router.add_get("/rest/bringusersettings/{user}", self._settings)
            app.router.add_get("/rest/bringusers/{user}/lists", self._lists)
            app.router.add_get("/rest/v2/bringlists/{list}", self._get_list)
            app.router.add_put("/rest/v2/bringlists/{list}/items", self._put)
            self._runner = web.AppRunner(app)
            self._loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            self._loop.run_until_complete(site.start())
            port = self._runner.addresses[0][1]
            self.base_url = f"http://127.0.0.1:{port}/rest/"
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        assert ready.wait(10), "fake Bring! server did not start"

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    # --- handlers --------------------------------------------------------

    async def _auth(self, request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("email") != self.email or form.get("password") != self.password:
            return web.json_response(
                {"message": "bad credentials", "error": "x", "error_description": "x", "errorcode": 1},
                status=401,
            )
        self.login_count += 1
        return web.json_response(
            {
                "uuid": self.user_uuid,
                "publicUuid": "public-uuid",
                "bringListUUID": self.default_list,
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )

    async def _account(self, request: web.Request) -> web.Response:
        language, country = self.locale.split("-")
        return web.json_response(
            {
                "email": self.email,
                "emailVerified": True,
                "premiumConfiguration": {},
                "publicUserUuid": "public-uuid",
                "userLocale": {"language": language, "country": country},
                "userUuid": self.user_uuid,
            }
        )

    async def _settings(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "usersettings": [{"key": "defaultListUUID", "value": self.default_list}],
                "userlistsettings": [
                    {"listUuid": lst["listUuid"], "usersettings": [{"key": "listArticleLanguage", "value": self.locale}]}
                    for lst in self.lists
                ],
            }
        )

    async def _lists(self, request: web.Request) -> web.Response:
        return web.json_response({"lists": self.lists})

    async def _get_list(self, request: web.Request) -> web.Response:
        if self.fail_gets:
            self.fail_gets -= 1
            return web.Response(status=500, text="boom")
        list_uuid = request.match_info["list"]
        purchase = [
            {"uuid": str(uuid.uuid4()), "itemId": item_id, "specification": spec}
            for item_id, spec in self.purchase[list_uuid].items()
        ]
        return web.json_response(
            {"uuid": list_uuid, "status": "SHARED", "items": {"purchase": purchase, "recently": []}}
        )

    async def _put(self, request: web.Request) -> web.Response:
        list_uuid = request.match_info["list"]
        body = await request.json()
        self.put_requests.append(copy.deepcopy(body))
        if self.fail_puts:
            self.fail_puts -= 1
            return web.Response(status=500, text="boom")
        changes = body["changes"]
        if any(change["itemId"] in self.reject_items for change in changes):
            return web.Response(status=500, text="rejected")
        for change in changes:
            if change["operation"] == "TO_PURCHASE":
                self.purchase[list_uuid][change["itemId"]] = change.get("spec", "")
            elif change["operation"] in {"REMOVE", "TO_RECENTLY"}:
                self.purchase[list_uuid].pop(change["itemId"], None)
        return web.Response(status=204)


@pytest.fixture
def bring_server(monkeypatch: pytest.MonkeyPatch):
    server = FakeBringServer()
    server.start()
    monkeypatch.setattr(bring_api.bring, "API_BASE_URL", server.base_url)
    try:
        yield server
    finally:
        server.stop()
