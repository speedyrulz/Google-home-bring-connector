import os
import time

import gpsoauth
import pytest

from keep_to_bring import cli
from keep_to_bring.config import parse_env_file


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    # Make sure the developer's real environment can't leak into the tests.
    for key in list(os.environ):
        if key.startswith(("GOOGLE_", "BRING_", "KEEP_", "POLL_", "DATA_DIR", "SPLIT_", "PARSE_", "ALERT_")):
            monkeypatch.delenv(key, raising=False)
    return tmp_path / ".env"


def write_config(env_file, bring_server, keep_server, tmp_path, **extra):
    keep_server.valid_tokens.add("aas_et/valid")
    lines = {
        "GOOGLE_EMAIL": "me@example.com",
        "GOOGLE_MASTER_TOKEN": "aas_et/valid",
        "BRING_EMAIL": bring_server.email,
        "BRING_PASSWORD": bring_server.password,
        "DATA_DIR": str(tmp_path / "data"),
        **extra,
    }
    env_file.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 1
    assert "setup" in capsys.readouterr().out


def test_missing_config_is_explained(env_file, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--env-file", str(env_file), "once"])
    assert info.value.code == 2
    assert "keep-to-bring setup" in capsys.readouterr().err


def test_once_moves_items(env_file, bring_server, keep_server, tmp_path):
    list_id = keep_server.add_list("Shopping list", ["milk"])
    write_config(env_file, bring_server, keep_server, tmp_path)
    assert cli.main(["--env-file", str(env_file), "once"]) == 0
    assert bring_server.purchase["list-home"] == {"Milch": ""}
    assert keep_server.live_texts(list_id) == []
    assert (tmp_path / "data" / "heartbeat").exists()


def test_once_fails_on_bad_google_token(env_file, bring_server, keep_server, tmp_path):
    write_config(env_file, bring_server, keep_server, tmp_path, GOOGLE_MASTER_TOKEN="aas_et/revoked")
    assert cli.main(["--env-file", str(env_file), "once"]) == 1


def test_check_changes_nothing(env_file, bring_server, keep_server, tmp_path, capsys):
    list_id = keep_server.add_list("Shopping list", ["2 gallons of milk"])
    write_config(env_file, bring_server, keep_server, tmp_path)
    assert cli.main(["--env-file", str(env_file), "check"]) == 0
    out = capsys.readouterr().out
    assert "'2 gallons of milk' -> Bring!: Milk (2 gallons)" in out
    assert bring_server.purchase["list-home"] == {}
    assert keep_server.live_texts(list_id) == ["2 gallons of milk"]


def test_health(env_file, tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    env_file.write_text(f"DATA_DIR={data}\nPOLL_INTERVAL_SECONDS=60\n", encoding="utf-8")
    args = ["--env-file", str(env_file), "health"]
    assert cli.main(args) == 1
    data.mkdir()
    heartbeat = data / "heartbeat"
    heartbeat.write_text("ok")
    assert cli.main(args) == 0
    old = time.time() - 3600
    os.utime(heartbeat, (old, old))
    assert cli.main(args) == 1


def answers(monkeypatch, typed, hidden):
    typed, hidden = iter(typed), iter(hidden)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(hidden))


def test_setup_wizard_writes_working_config(env_file, bring_server, keep_server, tmp_path, monkeypatch):
    keep_server.add_list("Shopping list", ["milk"])
    monkeypatch.setattr(gpsoauth, "exchange_token", lambda email, cookie, device: {"Token": "aas_et/from-cookie"})
    keep_server.valid_tokens.add("aas_et/from-cookie")
    answers(
        monkeypatch,
        typed=[
            bring_server.email,  # Bring! email
            "2",  # pick the second list ("Cabin")
            "",  # Keep list name: accept default
            "me@example.com",  # Google account
            "oauth2_4/cookie",  # pasted cookie
            "n",  # no more accounts
        ],
        hidden=[bring_server.password],
    )
    assert cli.main(["--env-file", str(env_file), "setup"]) == 0

    saved = parse_env_file(env_file)
    assert saved["BRING_EMAIL"] == bring_server.email
    assert saved["BRING_PASSWORD"] == bring_server.password
    assert saved["BRING_LIST_NAME"] == "Cabin"
    assert saved["KEEP_LIST_TITLES"] == "Shopping list"
    assert saved["GOOGLE_EMAIL"] == "me@example.com"
    assert saved["GOOGLE_MASTER_TOKEN"] == "aas_et/from-cookie"
    assert "KEEP THIS FILE PRIVATE" in env_file.read_text()

    # The written file works.
    with env_file.open("a") as handle:
        handle.write(f"DATA_DIR={tmp_path / 'data'}\n")
    assert cli.main(["--env-file", str(env_file), "once"]) == 0
    assert bring_server.purchase["list-cabin"] == {"Milch": ""}


def test_get_token_updates_existing_account(env_file, bring_server, keep_server, tmp_path, monkeypatch):
    write_config(env_file, bring_server, keep_server, tmp_path, GOOGLE_MASTER_TOKEN="aas_et/revoked")
    monkeypatch.setattr(gpsoauth, "exchange_token", lambda email, cookie, device: {"Token": "aas_et/renewed"})
    keep_server.valid_tokens.add("aas_et/renewed")
    answers(monkeypatch, typed=["", "oauth2_4/cookie"], hidden=[])
    assert cli.main(["--env-file", str(env_file), "get-token"]) == 0
    saved = parse_env_file(env_file)
    assert saved["GOOGLE_MASTER_TOKEN"] == "aas_et/renewed"
    assert saved["BRING_PASSWORD"] == bring_server.password  # untouched
