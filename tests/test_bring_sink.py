import aiohttp
import pytest

from keep_to_bring.bring_sink import BringAuthError, BringListNotFound, BringSink


async def connected(server, list_name=None) -> tuple[BringSink, aiohttp.ClientSession]:
    session = aiohttp.ClientSession()
    sink = BringSink(server.email, server.password, list_name, session)
    await sink.connect()
    return sink, session


async def test_connect_uses_default_list_and_catalogue(bring_server):
    bring_server.default_list = "list-cabin"
    sink, session = await connected(bring_server)
    try:
        assert sink.list_name == "Cabin"
        assert sink.catalog.locale == "en-US"
        assert sink.catalog.match("milk") == "Milk"
        assert [info.name for info in sink.lists] == ["Home", "Cabin"]
    finally:
        await session.close()


async def test_connect_by_name_is_case_insensitive(bring_server):
    sink, session = await connected(bring_server, "home")
    try:
        assert sink.list_name == "Home"
    finally:
        await session.close()


async def test_unknown_list_name_lists_alternatives(bring_server):
    async with aiohttp.ClientSession() as session:
        sink = BringSink(bring_server.email, bring_server.password, "Groceries", session)
        with pytest.raises(BringListNotFound, match="'Home', 'Cabin'"):
            await sink.connect()


async def test_wrong_password(bring_server):
    async with aiohttp.ClientSession() as session:
        sink = BringSink(bring_server.email, "wrong", None, session)
        with pytest.raises(BringAuthError, match="Change password"):
            await sink.connect()


async def test_add_translates_to_catalogue_key(bring_server):
    sink, session = await connected(bring_server)
    try:
        await sink.add("Milk", "2 gallons")
        await sink.add("Rex chew toy", "")
        # Bring stores catalogue items under their (Swiss German) key, so the
        # app shows the right icon and aisle.
        assert bring_server.purchase["list-home"] == {"Milch": "2 gallons", "Rex chew toy": ""}
        assert await sink.current_items() == {"milk": "2 gallons", "rex chew toy": ""}
    finally:
        await session.close()


async def test_remove(bring_server):
    bring_server.purchase["list-home"] = {"Milch": ""}
    sink, session = await connected(bring_server)
    try:
        await sink.remove("Milk")
        assert bring_server.purchase["list-home"] == {}
    finally:
        await session.close()


async def test_network_error_is_reported_as_unavailable(monkeypatch):
    import bring_api.bring

    from keep_to_bring.bring_sink import BringUnavailable

    # Nothing listens on port 9 (discard) on localhost.
    monkeypatch.setattr(bring_api.bring, "API_BASE_URL", "http://127.0.0.1:9/rest/")
    async with aiohttp.ClientSession() as session:
        sink = BringSink("a@b.c", "pw", None, session)
        with pytest.raises(BringUnavailable, match="internet connection"):
            await sink.connect()
