import pytest
import requests

from keep_to_bring.config import GoogleAccount, default_device_id
from keep_to_bring.keep_source import AUTH_RETRY_BASE_SECONDS, KeepAuthError, KeepSource


def texts(entries):
    return sorted(e.text for e in entries)


def test_pull_returns_unchecked_items_of_matching_lists_only(keep_server, google_account):
    keep_server.add_list("Shopping list", ["milk", ("bread", True), "eggs", "  "])
    keep_server.add_list("Hardware store", ["screws"])
    keep_server.add_note("Shopping list", "a plain note with the same title")
    # A trashed list with the same title must be ignored.
    keep_server.add_list("Shopping list", ["old"], timestamps={
        "kind": "notes#timestamps", "created": "2024-01-01T00:00:00.000000Z",
        "updated": "2024-01-01T00:00:00.000000Z", "trashed": "2024-02-01T00:00:00.000000Z",
    })

    source = KeepSource(google_account, ["shopping LIST"])
    entries = source.pull()

    assert texts(entries) == ["eggs", "milk"]
    assert {e.list_title for e in entries} == {"Shopping list"}
    assert {e.account for e in entries} == {"me@example.com"}


def test_multiple_titles(keep_server, google_account):
    keep_server.add_list("Shopping list", ["milk"])
    keep_server.add_list("Groceries", ["eggs"])
    source = KeepSource(google_account, ["Shopping list", "Groceries"])
    assert texts(source.pull()) == ["eggs", "milk"]


def test_delete_after_transfer_is_synced_to_google(keep_server, google_account):
    list_id = keep_server.add_list("Shopping list", ["milk", "eggs"])
    source = KeepSource(google_account, ["Shopping list"])
    entries = source.pull()

    milk = [e for e in entries if e.text == "milk"]
    assert source.mark_done(milk) == 1
    source.push()

    assert keep_server.live_texts(list_id) == ["eggs"]
    # The deletion was sent to the server as a node update.
    sent = [n for req in keep_server.requests for n in req.get("nodes", [])]
    assert any(n["text"] == "milk" and n["timestamps"].get("deleted") for n in sent)
    assert texts(source.pull()) == ["eggs"]


def test_check_action_ticks_items_instead(keep_server, google_account):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    source = KeepSource(google_account, ["Shopping list"], action="check")
    source.mark_done(source.pull())
    source.push()
    (item,) = keep_server.items(list_id)
    assert item["checked"] is True
    assert source.pull() == []


def test_incremental_sync_sees_new_voice_items(keep_server, google_account):
    list_id = keep_server.add_list("Shopping list", [])
    source = KeepSource(google_account, ["Shopping list"])
    assert source.pull() == []
    keep_server.add_item(list_id, "bananas")
    assert texts(source.pull()) == ["bananas"]
    # Second request was incremental (sent a targetVersion).
    assert keep_server.requests[-1].get("targetVersion")


def test_full_resync_when_google_asks(keep_server, google_account):
    keep_server.add_list("Shopping list", ["milk"])
    source = KeepSource(google_account, ["Shopping list"])
    source.pull()
    keep_server.force_resync_next = True
    assert texts(source.pull()) == ["milk"]


def test_bad_token_raises_auth_error_with_backoff(keep_server):
    now = [1000.0]
    account = GoogleAccount("me@example.com", "aas_et/revoked", default_device_id("me@example.com"))
    source = KeepSource(account, ["Shopping list"], clock=lambda: now[0])

    with pytest.raises(KeepAuthError, match="get-token"):
        source.pull()
    assert source.auth_broken
    assert source.waiting_for_retry()
    now[0] += AUTH_RETRY_BASE_SECONDS + 1
    assert not source.waiting_for_retry()

    with pytest.raises(KeepAuthError):
        source.pull()
    # Backoff doubles after repeated failures.
    now[0] += AUTH_RETRY_BASE_SECONDS + 1
    assert source.waiting_for_retry()


def test_update_account_recovers(keep_server, google_account):
    keep_server.add_list("Shopping list", ["milk"])
    bad = GoogleAccount(google_account.email, "aas_et/old", google_account.device_id)
    source = KeepSource(bad, ["Shopping list"])
    with pytest.raises(KeepAuthError):
        source.pull()
    source.update_account(google_account)
    assert not source.auth_broken and not source.waiting_for_retry()
    assert texts(source.pull()) == ["milk"]


def test_network_errors_propagate_as_is(keep_server, google_account):
    source = KeepSource(google_account, ["Shopping list"])
    keep_server.fail_next.append(requests.ConnectionError("offline"))
    with pytest.raises(requests.ConnectionError):
        source.pull()
    assert not source.auth_broken


def test_missing_list_warns_once(keep_server, google_account, caplog):
    source = KeepSource(google_account, ["Shopping list"])
    source.pull()
    source.pull()
    warnings = [r for r in caplog.records if "No Google Keep checklist" in r.message]
    assert len(warnings) == 1
