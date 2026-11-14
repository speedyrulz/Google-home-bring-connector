"""Getting a Google "master token" for Google Keep.

Google no longer allows logging in to Keep with just an email and password
from a script. The workaround used by gkeepapi, Home Assistant add-ons, etc.:

1. Sign in on https://accounts.google.com/EmbeddedSetup in a normal browser.
2. Copy the short-lived ``oauth_token`` cookie that page sets.
3. Exchange it (once) for a long-lived master token.

The master token grants broad access to the Google account, so treat it like
a password.
"""

from __future__ import annotations

import gpsoauth

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"

COOKIE_INSTRUCTIONS = f"""\
How to get the one-time Google sign-in code (takes about 2 minutes):

  1. On a computer, open a PRIVATE / INCOGNITO browser window
     (Chrome or Edge: Ctrl+Shift+N, Mac: Cmd+Shift+N; Firefox: Ctrl+Shift+P).
  2. Go to:  {EMBEDDED_SETUP_URL}
  3. Sign in with the Google account your Google Home uses for the
     shopping list. If asked, click "I agree". The page may then keep
     loading forever - that's expected, leave it open.
  4. Open the browser's developer tools: press F12
     (Mac: Cmd+Option+I).
  5. Chrome/Edge: click the "Application" tab, then in the left column
     Storage > Cookies > https://accounts.google.com
     Firefox: click the "Storage" tab, then Cookies > https://accounts.google.com
     (If you can't see the tab, click the >> arrows at the top.)
  6. Click the row named  oauth_token  and copy its Value
     (it starts with "oauth2_4/"). Double-click the value to select it all.
  7. Paste it below within a few minutes - the code works only once and
     expires quickly. If it fails, repeat these steps to get a fresh one.
"""


class TokenExchangeError(Exception):
    """Google refused to exchange the cookie for a master token."""


def clean_oauth_cookie(raw: str) -> str:
    token = raw.strip().strip("'\"").strip()
    if token.lower().startswith("oauth_token="):
        token = token.split("=", 1)[1]
    token = token.split(";", 1)[0].strip()
    return token


def exchange_oauth_cookie(email: str, oauth_cookie: str, device_id: str) -> str:
    """Exchange the browser ``oauth_token`` cookie for a master token."""
    token = clean_oauth_cookie(oauth_cookie)
    if not token:
        raise TokenExchangeError("No code was pasted.")
    if not token.startswith("oauth2_4/"):
        raise TokenExchangeError(
            "That doesn't look like the oauth_token cookie (it should start with "
            "'oauth2_4/'). Make sure you copied the Value of the row named oauth_token."
        )
    try:
        response = gpsoauth.exchange_token(email.strip(), token, device_id)
    except Exception as err:  # noqa: BLE001 - network errors etc.
        raise TokenExchangeError(f"Could not reach Google: {err}") from err
    master_token = response.get("Token")
    if not master_token:
        error = response.get("Error") or "unknown error"
        hint = (
            " The code has probably expired or was already used - get a fresh one and "
            "paste it right away. Also check that the email matches the account you "
            "signed in with."
            if error == "BadAuthentication"
            else ""
        )
        raise TokenExchangeError(f"Google refused the code ({error}).{hint}")
    return master_token
