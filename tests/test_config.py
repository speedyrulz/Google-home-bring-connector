import os
import stat
from pathlib import Path

import pytest

from keep_to_bring.config import (
    ConfigError,
    default_device_id,
    load_environment,
    load_settings,
    parse_env_file,
    resolve_env_file,
    write_env_file,
)

MINIMAL = {
    "GOOGLE_EMAIL": "me@example.com",
    "GOOGLE_MASTER_TOKEN": "aas_et/abc",
    "BRING_EMAIL": "bring@example.com",
    "BRING_PASSWORD": "pw",
}


def test_minimal_settings_and_defaults():
    settings = load_settings(MINIMAL)
    assert [a.email for a in settings.google_accounts] == ["me@example.com"]
    assert settings.google_accounts[0].device_id == default_device_id("me@example.com")
    assert settings.keep_list_titles == ("Shopping list",)
    assert settings.poll_interval == 60
    assert settings.keep_action == "delete"
    assert settings.parse_quantities is True
    assert settings.split_items is False
    assert settings.alert_in_bring is True
    assert settings.bring_list_name is None
    assert settings.heartbeat_file == Path("data") / "heartbeat"


def test_secrets_are_not_in_repr():
    text = repr(load_settings(MINIMAL))
    assert "aas_et/abc" not in text
    assert "'pw'" not in text


def test_multiple_accounts_and_options():
    env = dict(
        MINIMAL,
        GOOGLE_EMAIL_2="partner@example.com",
        GOOGLE_MASTER_TOKEN_2="aas_et/def",
        KEEP_LIST_TITLES="Shopping list, Groceries",
        BRING_LIST_NAME="Home",
        POLL_INTERVAL_SECONDS="30",
        KEEP_ACTION="check",
        PARSE_QUANTITIES="no",
        SPLIT_ITEMS="yes",
        ALERT_IN_BRING="off",
        DATA_DIR="/data",
    )
    settings = load_settings(env)
    assert [a.email for a in settings.google_accounts] == ["me@example.com", "partner@example.com"]
    assert settings.keep_list_titles == ("Shopping list", "Groceries")
    assert settings.bring_list_name == "Home"
    assert settings.poll_interval == 30
    assert settings.keep_action == "check"
    assert settings.parse_quantities is False
    assert settings.split_items is True
    assert settings.alert_in_bring is False
    assert settings.data_dir == Path("/data")


def test_all_errors_reported_together():
    with pytest.raises(ConfigError) as info:
        load_settings({"POLL_INTERVAL_SECONDS": "5", "KEEP_ACTION": "archive", "SPLIT_ITEMS": "maybe"})
    message = str(info.value)
    for fragment in ("No Google account", "BRING_EMAIL", "BRING_PASSWORD", "at least 15", "KEEP_ACTION", "SPLIT_ITEMS"):
        assert fragment in message


def test_cookie_instead_of_master_token_is_explained():
    with pytest.raises(ConfigError, match="oauth_token cookie"):
        load_settings(dict(MINIMAL, GOOGLE_MASTER_TOKEN="oauth2_4/xyz"))


def test_half_configured_account():
    with pytest.raises(ConfigError, match="GOOGLE_MASTER_TOKEN_2 is missing"):
        load_settings(dict(MINIMAL, GOOGLE_EMAIL_2="partner@example.com"))


def test_parse_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "\n"
        "export PLAIN=value\n"
        "SPACED = value with spaces  # trailing comment\n"
        "SINGLE='it''s # not a comment'\n"
        'DOUBLE="say \\"hi\\" \\\\ there"\n'
        "TOKEN=aas_et/AbC-123_=\n"
        "EMPTY=\n"
        "   # indented comment\n"
        "not a valid line\n",
        encoding="utf-8",
    )
    assert parse_env_file(path) == {
        "PLAIN": "value",
        "SPACED": "value with spaces",
        "SINGLE": "it''s # not a comment",
        "DOUBLE": 'say "hi" \\ there',
        "TOKEN": "aas_et/AbC-123_=",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    "value",
    ["simple", "aas_et/AbC-123_=", "with space", "p@ss#word", "it's", 'quote"d', "back\\slash", "$HOME", ""],
)
def test_write_then_parse_round_trip(tmp_path, value):
    path = tmp_path / ".env"
    write_env_file(path, {"KEY": value})
    assert parse_env_file(path).get("KEY", "") == value


def test_write_env_file_updates_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# my notes\nA=1\nB=2\n# C=commented\n", encoding="utf-8")
    write_env_file(path, {"B": "two", "C": "3", "A": None})
    assert path.read_text(encoding="utf-8") == "# my notes\nB=two\n# C=commented\nC=3\n"


def test_write_env_file_header_and_permissions(tmp_path):
    path = tmp_path / "sub" / ".env"
    write_env_file(path, {"A": "1"}, header="# header line\n")
    assert path.read_text(encoding="utf-8") == "# header line\n\nA=1\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_real_environment_wins_but_empty_values_do_not_mask(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=file\nB=file\nC=file\n", encoding="utf-8")
    merged = load_environment(path, {"A": "env", "B": "", "D": ""})
    assert merged["A"] == "env"
    assert merged["B"] == "file"
    assert merged["C"] == "file"
    assert merged["D"] == ""


def test_missing_env_file_is_fine(tmp_path):
    assert load_environment(tmp_path / "nope", {"X": "1"}) == {"X": "1"}


def test_resolve_env_file():
    assert resolve_env_file("custom.env", {}) == Path("custom.env")
    assert resolve_env_file(None, {"KEEP_TO_BRING_ENV_FILE": "/data/.env"}) == Path("/data/.env")
    assert resolve_env_file(None, {}) == Path(".env")


def test_device_id_is_stable_and_android_shaped():
    assert default_device_id("Me@Example.com ") == default_device_id("me@example.com")
    assert len(default_device_id("me@example.com")) == 16
    int(default_device_id("me@example.com"), 16)


def test_write_env_file_removes_duplicate_keys(tmp_path):
    # A hand-edited file with the same key twice: the last one would win when
    # read back, so writing must replace *all* of them.
    path = tmp_path / ".env"
    path.write_text("GOOGLE_MASTER_TOKEN=aas_et/old1\nX=1\nGOOGLE_MASTER_TOKEN=aas_et/old2\n", encoding="utf-8")
    write_env_file(path, {"GOOGLE_MASTER_TOKEN": "aas_et/new"})
    assert parse_env_file(path) == {"GOOGLE_MASTER_TOKEN": "aas_et/new", "X": "1"}
    assert path.read_text(encoding="utf-8").count("GOOGLE_MASTER_TOKEN") == 1


def test_quoted_value_with_trailing_comment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A='my pass' # note\nB=\"say \\\"hi\\\"\"  # note\n", encoding="utf-8")
    assert parse_env_file(path) == {"A": "my pass", "B": 'say "hi"'}
