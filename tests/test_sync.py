"""End-to-end tests of the sync engine: real gkeepapi + real bring-api, fake servers."""

import asyncio

import aiohttp
import pytest
import requests

from keep_to_bring.bring_sink import BringAuthError, BringSink
from keep_to_bring.config import GoogleAccount, default_device_id
from keep_to_bring.keep_source import KeepSource
from keep_to_bring.sync import ALERT_ITEM_NAME, Connector, merge_spec, run_forever


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def make_connector(bring_server, session, accounts, *, titles=("Shopping list",), **kwargs):
    sink = BringSink(bring_server.email, bring_server.password, None, session)
    sources = [KeepSource(a, titles) for a in accounts]
    return Connector(sources, sink, **kwargs)


def account(keep_server, email, token):
    keep_server.valid_tokens.add(token)
    return GoogleAccount(email, token, default_device_id(email))


async def test_voice_items_move_from_keep_to_bring(keep_server, bring_server, google_account, session):
    list_id = keep_server.add_list(
        "Shopping list", ["milk", "2 gallons of orange juice", "a dozen eggs", "rex chew toy", ("bread", True)]
    )
    connector = make_connector(bring_server, session, [google_account])

    report = await connector.sync_once()

    assert bring_server.purchase["list-home"] == {
        "Milch": "",
        "Orangensaft": "2 gallons",
        "Eier": "1 dozen",
        "Rex chew toy": "",
    }
    assert sorted(report.added) == ["Eggs (1 dozen)", "Milk", "Orange juice (2 gallons)", "Rex chew toy"]
    assert report.removed_from_keep == 4
    assert report.healthy
    # Only the checked-off item is left in Keep.
    assert keep_server.live_texts(list_id) == []
    assert [n["text"] for n in keep_server.items(list_id) if n.get("checked")] == ["bread"]


async def test_new_items_picked_up_on_later_cycles(keep_server, bring_server, google_account, session):
    list_id = keep_server.add_list("Shopping list", [])
    connector = make_connector(bring_server, session, [google_account])
    assert (await connector.sync_once()).added == []
    assert bring_server.put_requests == []

    keep_server.add_item(list_id, "bananas")
    report = await connector.sync_once()
    assert report.added == ["Bananas"]
    assert bring_server.purchase["list-home"] == {"Bananen": ""}
    assert keep_server.live_texts(list_id) == []


async def test_items_already_on_bring_are_not_duplicated_or_overwritten(
    keep_server, bring_server, google_account, session
):
    bring_server.purchase["list-home"] = {"Milch": "2% organic", "Eier": ""}
    list_id = keep_server.add_list("Shopping list", ["milk", "eggs", "2 gallons of milk"])
    connector = make_connector(bring_server, session, [google_account])

    report = await connector.sync_once()

    # "milk" keeps the partner's note; "2 gallons of milk" is appended to it.
    assert bring_server.purchase["list-home"] == {"Milch": "2% organic, 2 gallons", "Eier": ""}
    assert sorted(report.already_on_list) == ["Eggs", "Milk"]
    assert report.updated == ["Milk (2% organic, 2 gallons)"]
    assert keep_server.live_texts(list_id) == []


async def test_bring_failure_keeps_items_in_keep_and_retries(keep_server, bring_server, google_account, session):
    list_id = keep_server.add_list("Shopping list", ["milk", "eggs"])
    bring_server.reject_items = {"Eier"}
    connector = make_connector(bring_server, session, [google_account])

    report = await connector.sync_once()
    assert report.added == ["Milk"]
    assert report.failed == ["Eggs"]
    assert not report.healthy
    assert keep_server.live_texts(list_id) == ["eggs"]  # not lost

    bring_server.reject_items = set()
    report = await connector.sync_once()
    assert report.added == ["Eggs"]
    assert report.healthy
    assert keep_server.live_texts(list_id) == []
    assert bring_server.purchase["list-home"] == {"Milch": "", "Eier": ""}


async def test_bring_unreachable_removes_nothing(keep_server, bring_server, google_account, session):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    connector = make_connector(bring_server, session, [google_account])
    await connector.ensure_connected()
    bring_server.fail_gets = 1
    with pytest.raises(Exception):
        await connector.sync_once()
    assert keep_server.live_texts(list_id) == ["milk"]
    await connector.sync_once()
    assert keep_server.live_texts(list_id) == []


async def test_keep_push_failure_is_retried_without_duplicates(keep_server, bring_server, google_account, session):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    connector = make_connector(bring_server, session, [google_account])
    await connector.sync_once()  # connect + full sync; moves milk
    keep_server.add_item(list_id, "eggs")

    # Pull works, but the request that clears "eggs" from Keep fails.
    original = keep_server.handle
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise requests.ConnectionError("network blip")
        return original(**kwargs)

    keep_server.handle = flaky
    report = await connector.sync_once()
    assert report.added == ["Eggs"]
    assert report.removed_from_keep == 0
    assert not report.healthy
    assert keep_server.live_texts(list_id) == ["eggs"]

    report = await connector.sync_once()
    assert keep_server.live_texts(list_id) == []
    assert report.added == []
    assert bring_server.purchase["list-home"] == {"Milch": "", "Eier": ""}


async def test_two_accounts_and_alert_when_one_login_breaks(keep_server, bring_server, session):
    me = account(keep_server, "me@example.com", "aas_et/me")
    partner_good = account(keep_server, "partner@example.com", "aas_et/partner-new")
    partner_bad = GoogleAccount("partner@example.com", "aas_et/partner-old", partner_good.device_id)
    list_id = keep_server.add_list("Shopping list", ["milk"])

    fresh = {}
    connector = make_connector(bring_server, session, [me, partner_bad], reload_accounts=lambda: fresh)

    report = await connector.sync_once()
    # My items still sync even though the partner's login is broken.
    assert report.added == ["Milk"]
    assert report.auth_failures == ["partner@example.com"]
    assert not report.healthy
    assert bring_server.purchase["list-home"][ALERT_ITEM_NAME].endswith("partner@example.com")

    # The alert is not re-sent every cycle.
    puts = len(bring_server.put_requests)
    await connector.sync_once()
    assert len(bring_server.put_requests) == puts

    # User runs `get-token`; the connector picks up the new token by itself.
    fresh["partner@example.com"] = partner_good
    keep_server.add_item(list_id, "eggs")
    report = await connector.sync_once()
    assert report.healthy
    assert ALERT_ITEM_NAME not in bring_server.purchase["list-home"]
    assert bring_server.purchase["list-home"] == {"Milch": "", "Eier": ""}


async def test_leftover_alert_removed_after_restart(keep_server, bring_server, google_account, session):
    bring_server.purchase["list-home"] = {ALERT_ITEM_NAME: "old", "Milch": ""}
    keep_server.add_list("Shopping list", [])
    connector = make_connector(bring_server, session, [google_account])
    await connector.sync_once()
    assert bring_server.purchase["list-home"] == {"Milch": ""}


async def test_alerts_can_be_disabled(keep_server, bring_server, session):
    bad = GoogleAccount("me@example.com", "aas_et/bad", default_device_id("me@example.com"))
    connector = make_connector(bring_server, session, [bad], alert_in_bring=False)
    report = await connector.sync_once()
    assert report.auth_failures == ["me@example.com"]
    assert bring_server.purchase["list-home"] == {}


async def test_split_items_option(keep_server, bring_server, google_account, session):
    keep_server.add_list("Shopping list", ["milk, eggs and bread", "half and half"])
    connector = make_connector(bring_server, session, [google_account], split_items=True)
    await connector.sync_once()
    assert bring_server.purchase["list-home"] == {"Milch": "", "Eier": "", "Brot": "", "Half and half": ""}


async def test_quantity_parsing_can_be_disabled(keep_server, bring_server, google_account, session):
    keep_server.add_list("Shopping list", ["2 gallons of milk"])
    connector = make_connector(bring_server, session, [google_account], parse_quantities=False)
    await connector.sync_once()
    assert bring_server.purchase["list-home"] == {"2 gallons of milk": ""}


async def test_wrong_bring_password_raises(keep_server, bring_server, google_account, session):
    keep_server.add_list("Shopping list", ["milk"])
    sink = BringSink(bring_server.email, "wrong", None, session)
    connector = Connector([KeepSource(google_account, ["Shopping list"])], sink)
    with pytest.raises(BringAuthError):
        await connector.sync_once()


async def test_run_forever_writes_heartbeat_and_stops(keep_server, bring_server, google_account, session, tmp_path):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    connector = make_connector(bring_server, session, [google_account])
    stop = asyncio.Event()
    heartbeat = tmp_path / "heartbeat"

    task = asyncio.create_task(run_forever(connector, interval=0.05, heartbeat_file=heartbeat, stop=stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if heartbeat.exists():
            break
    keep_server.add_item(list_id, "eggs")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if "Eier" in bring_server.purchase["list-home"]:
            break
    stop.set()
    await asyncio.wait_for(task, 5)

    assert heartbeat.exists()
    assert bring_server.purchase["list-home"] == {"Milch": "", "Eier": ""}


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        ("", "", ""),
        ("", "2", "2"),
        ("organic", "", "organic"),
        ("organic", "2", "organic, 2"),
        ("2 gallons", "2 gallons", "2 gallons"),
        ("organic, 2 gallons", "2 Gallons", "organic, 2 gallons"),
    ],
)
def test_merge_spec(current, new, expected):
    assert merge_spec(current, new) == expected
