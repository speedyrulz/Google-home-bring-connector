import gpsoauth
import pytest

from keep_to_bring.google_token import TokenExchangeError, clean_oauth_cookie, exchange_oauth_cookie


@pytest.mark.parametrize(
    "raw",
    [
        "oauth2_4/abc",
        "  oauth2_4/abc  ",
        '"oauth2_4/abc"',
        "oauth_token=oauth2_4/abc",
        "oauth_token=oauth2_4/abc; Path=/; Secure",
    ],
)
def test_clean_cookie(raw):
    assert clean_oauth_cookie(raw) == "oauth2_4/abc"


def test_exchange_success(monkeypatch):
    calls = []

    def fake_exchange(email, token, android_id):
        calls.append((email, token, android_id))
        return {"Token": "aas_et/master"}

    monkeypatch.setattr(gpsoauth, "exchange_token", fake_exchange)
    assert exchange_oauth_cookie(" me@example.com ", "oauth_token=oauth2_4/abc", "0123456789abcdef") == "aas_et/master"
    assert calls == [("me@example.com", "oauth2_4/abc", "0123456789abcdef")]


def test_exchange_rejected_explains_expiry(monkeypatch):
    monkeypatch.setattr(gpsoauth, "exchange_token", lambda *a: {"Error": "BadAuthentication"})
    with pytest.raises(TokenExchangeError, match="expired or was already used"):
        exchange_oauth_cookie("me@example.com", "oauth2_4/abc", "0123456789abcdef")


def test_exchange_network_error(monkeypatch):
    def boom(*args):
        raise ConnectionError("offline")

    monkeypatch.setattr(gpsoauth, "exchange_token", boom)
    with pytest.raises(TokenExchangeError, match="Could not reach Google"):
        exchange_oauth_cookie("me@example.com", "oauth2_4/abc", "0123456789abcdef")


@pytest.mark.parametrize("raw", ["", "   ", "aas_et/already-a-master-token", "random text"])
def test_wrong_value_pasted(raw):
    with pytest.raises(TokenExchangeError):
        exchange_oauth_cookie("me@example.com", raw, "0123456789abcdef")
