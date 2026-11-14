"""Configuration: environment variables, optionally loaded from a ``.env`` file.

Real environment variables always win over values in the ``.env`` file, so the
same code works with Docker, a plain ``.env`` file, or GitHub Actions secrets.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE_VAR = "KEEP_TO_BRING_ENV_FILE"
DEFAULT_ENV_FILE = ".env"
MAX_GOOGLE_ACCOUNTS = 10
MIN_POLL_INTERVAL = 15.0
DEFAULT_POLL_INTERVAL = 60.0
KEEP_ACTIONS = ("delete", "check")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


class ConfigError(Exception):
    """The configuration is missing or invalid. The message is user-facing."""


def default_device_id(email: str) -> str:
    """A stable, Android-style 16 hex digit device id derived from the email.

    Google ties a master token to the device id it was issued for, so the
    token helper and the running connector must use the same value. Deriving
    it from the email keeps it identical across machines and containers.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    master_token: str = field(repr=False)
    device_id: str


@dataclass(frozen=True)
class Settings:
    google_accounts: tuple[GoogleAccount, ...]
    bring_email: str
    bring_password: str = field(repr=False)
    bring_list_name: str | None = None
    keep_list_titles: tuple[str, ...] = ("Shopping list",)
    poll_interval: float = DEFAULT_POLL_INTERVAL
    keep_action: str = "delete"
    parse_quantities: bool = True
    split_items: bool = False
    alert_in_bring: bool = True
    data_dir: Path = Path("data")
    log_level: str = "INFO"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"


# --------------------------------------------------------------------------
# .env file handling
# --------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        inner = raw[1:-1]
        return re.sub(r'\\(["\\])', r"\1", inner)
    # Unquoted: allow trailing " # comment".
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` file. Blank lines and ``#`` comments are ignored."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def _quote(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+=,%^~-]+", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(path: Path, updates: Mapping[str, str | None], header: str | None = None) -> None:
    """Create or update an env file in place, keeping comments and other keys.

    A value of ``None`` removes the key. The file is created with owner-only
    permissions because it holds passwords and tokens.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not lines and header:
        lines = header.rstrip("\n").splitlines() + [""]
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and not line.strip().startswith("#") and match.group(1) in remaining:
            value = remaining.pop(match.group(1))
            if value is not None:
                out.append(f"{match.group(1)}={_quote(value)}")
            continue
        out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={_quote(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out).rstrip("\n") + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - e.g. some Windows filesystems
        pass


def resolve_env_file(explicit: str | None, environ: Mapping[str, str] = os.environ) -> Path:
    """Which env file to use: ``--env-file``, then $KEEP_TO_BRING_ENV_FILE, then ``.env``."""
    return Path(explicit or environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE)


def load_environment(env_file: Path | None, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Merge the env file (if present) with the real environment (which wins)."""
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        values.update(parse_env_file(env_file))
    for key, value in environ.items():
        # GitHub Actions passes unset secrets as empty strings; don't let an
        # empty variable mask a real value from the file.
        if value != "" or key not in values:
            values[key] = value
    return values


# --------------------------------------------------------------------------
# Settings validation
# --------------------------------------------------------------------------


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _parse_bool(env: Mapping[str, str], key: str, default: bool, errors: list[str]) -> bool:
    raw = _get(env, key).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    errors.append(f"{key} must be true or false (got {raw!r}).")
    return default


def _google_accounts(env: Mapping[str, str], errors: list[str]) -> list[GoogleAccount]:
    accounts: list[GoogleAccount] = []
    for index in range(1, MAX_GOOGLE_ACCOUNTS + 1):
        suffix = "" if index == 1 else f"_{index}"
        email_key, token_key = f"GOOGLE_EMAIL{suffix}", f"GOOGLE_MASTER_TOKEN{suffix}"
        email, token = _get(env, email_key), _get(env, token_key)
        if not email and not token:
            continue
        if not email:
            errors.append(f"{token_key} is set but {email_key} is missing.")
            continue
        if not token:
            errors.append(
                f"{email_key} is set but {token_key} is missing. "
                "Run `keep-to-bring get-token` to create one."
            )
            continue
        if token.startswith("oauth2_4/"):
            errors.append(
                f"{token_key} contains the browser oauth_token cookie, not a master token. "
                "Run `keep-to-bring get-token` to exchange the cookie for a master token."
            )
            continue
        device_id = _get(env, f"GOOGLE_DEVICE_ID{suffix}") or default_device_id(email)
        accounts.append(GoogleAccount(email=email, master_token=token, device_id=device_id))
    return accounts


def load_settings(env: Mapping[str, str]) -> Settings:
    """Validate the environment and build :class:`Settings`.

    All problems are collected and reported together in one :class:`ConfigError`.
    """
    errors: list[str] = []

    accounts = _google_accounts(env, errors)
    if not accounts and not errors:
        errors.append(
            "No Google account configured. Set GOOGLE_EMAIL and GOOGLE_MASTER_TOKEN "
            "(run `keep-to-bring setup`)."
        )

    bring_email, bring_password = _get(env, "BRING_EMAIL"), env.get("BRING_PASSWORD") or ""
    if not bring_email:
        errors.append("BRING_EMAIL is missing (the email address you log in to Bring! with).")
    if not bring_password:
        errors.append(
            "BRING_PASSWORD is missing. If you signed up for Bring! with Google or Apple, "
            "set a password first in the Bring! app: Profile > Settings > Change password."
        )

    titles = tuple(t.strip() for t in _get(env, "KEEP_LIST_TITLES").split(",") if t.strip())
    if not titles:
        titles = ("Shopping list",)

    poll_interval = DEFAULT_POLL_INTERVAL
    raw_interval = _get(env, "POLL_INTERVAL_SECONDS")
    if raw_interval:
        try:
            poll_interval = float(raw_interval)
        except ValueError:
            errors.append(f"POLL_INTERVAL_SECONDS must be a number (got {raw_interval!r}).")
        else:
            if poll_interval < MIN_POLL_INTERVAL:
                errors.append(
                    f"POLL_INTERVAL_SECONDS must be at least {MIN_POLL_INTERVAL:g} "
                    "to avoid being rate limited by Google."
                )

    keep_action = (_get(env, "KEEP_ACTION") or "delete").lower()
    if keep_action not in KEEP_ACTIONS:
        errors.append(f"KEEP_ACTION must be one of {', '.join(KEEP_ACTIONS)} (got {keep_action!r}).")

    parse_quantities = _parse_bool(env, "PARSE_QUANTITIES", True, errors)
    split_items = _parse_bool(env, "SPLIT_ITEMS", False, errors)
    alert_in_bring = _parse_bool(env, "ALERT_IN_BRING", True, errors)

    log_level = (_get(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        errors.append(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR (got {log_level!r}).")

    if errors:
        raise ConfigError("\n".join(f"  - {e}" for e in errors))

    return Settings(
        google_accounts=tuple(accounts),
        bring_email=bring_email,
        bring_password=bring_password,
        bring_list_name=_get(env, "BRING_LIST_NAME") or None,
        keep_list_titles=titles,
        poll_interval=poll_interval,
        keep_action=keep_action,
        parse_quantities=parse_quantities,
        split_items=split_items,
        alert_in_bring=alert_in_bring,
        data_dir=Path(_get(env, "DATA_DIR") or "data"),
        log_level=log_level,
    )
