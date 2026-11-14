"""Command line interface: ``keep-to-bring <command>``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import aiohttp

from . import __version__
from .bring_sink import BringAuthError, BringListNotFound, BringSink
from .config import (
    DEFAULT_POLL_INTERVAL,
    MAX_GOOGLE_ACCOUNTS,
    ConfigError,
    GoogleAccount,
    Settings,
    default_device_id,
    load_environment,
    load_settings,
    parse_env_file,
    resolve_env_file,
    write_env_file,
)
from .google_token import COOKIE_INSTRUCTIONS, TokenExchangeError, exchange_oauth_cookie
from .keep_source import KeepAuthError, KeepSource
from .net import run_blocking
from .sync import Connector, run_forever, write_heartbeat

_LOGGER = logging.getLogger("keep_to_bring")

ENV_HEADER = """\
# Keep-to-Bring configuration (created by `keep-to-bring setup`).
# KEEP THIS FILE PRIVATE: it contains your Bring! password and Google master token(s).
#
# Optional settings - remove the leading "# " to change one:
# POLL_INTERVAL_SECONDS=60   # how often to check Google Keep, in seconds (minimum 15)
# KEEP_ACTION=delete         # after copying an item to Bring!: delete it from Keep, or "check" it off
# PARSE_QUANTITIES=true      # "2 gallons of milk" -> "Milk" with the note "2 gallons"
# SPLIT_ITEMS=false          # "milk, eggs and bread" said as one item -> three items
# ALERT_IN_BRING=true        # put a reminder on the Bring! list if a Google login stops working
# LOG_LEVEL=INFO             # DEBUG for more detail when troubleshooting
"""


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if level != "DEBUG":
        for noisy in ("gkeepapi", "urllib3", "bring_api", "aiohttp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _load(args: argparse.Namespace) -> Settings:
    env_file = resolve_env_file(args.env_file)
    try:
        settings = load_settings(load_environment(env_file))
    except ConfigError as err:
        where = f" (read from {env_file})" if env_file.is_file() else f" ({env_file} not found)"
        print(f"Configuration problem{where}:\n{err}", file=sys.stderr)
        print("\nRun `keep-to-bring setup` to create the configuration.", file=sys.stderr)
        raise SystemExit(2) from None
    _setup_logging("DEBUG" if args.verbose else settings.log_level)
    return settings


def _sources(settings: Settings) -> list[KeepSource]:
    return [
        KeepSource(account, settings.keep_list_titles, settings.keep_action)
        for account in settings.google_accounts
    ]


def _account_reloader(args: argparse.Namespace):
    """Re-read the settings file so a token renewed with `get-token` is used without a restart."""
    env_file = resolve_env_file(args.env_file)

    def reload() -> dict[str, GoogleAccount]:
        try:
            settings = load_settings(load_environment(env_file))
        except ConfigError:
            return {}
        return {account.email.casefold(): account for account in settings.google_accounts}

    return reload


def _connector(settings: Settings, sink: BringSink, args: argparse.Namespace | None = None) -> Connector:
    return Connector(
        _sources(settings),
        sink,
        parse_quantities=settings.parse_quantities,
        split_items=settings.split_items,
        alert_in_bring=settings.alert_in_bring,
        reload_accounts=_account_reloader(args) if args is not None else None,
    )


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=60))


def _ask(prompt: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    while True:
        try:
            value = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        except EOFError:
            raise SystemExit("\nCancelled.") from None
        if value:
            return value
        if default:
            return default
        print("  (this is required)")


def _yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        raise SystemExit("\nCancelled.") from None
    if not answer:
        return default
    return answer.startswith("y")


def _heading(text: str) -> None:
    print(f"\n=== {text} " + "=" * max(0, 60 - len(text)))


# ----------------------------------------------------------------------
# Interactive pieces shared by `setup` and `get-token`
# ----------------------------------------------------------------------


def _obtain_master_token(email: str) -> str:
    print()
    print(COOKIE_INSTRUCTIONS)
    device_id = default_device_id(email)
    while True:
        cookie = _ask("Paste the oauth_token value here")
        try:
            token = exchange_oauth_cookie(email, cookie, device_id)
        except TokenExchangeError as err:
            print(f"\n  Problem: {err}\n")
            if not _yes("Try again with a fresh code?"):
                raise SystemExit(1) from None
            continue
        print("  Got a master token from Google.")
        return token


def _verify_keep(account: GoogleAccount, titles: Sequence[str]) -> bool:
    print(f"  Checking Google Keep for {account.email} ...")
    source = KeepSource(account, titles)
    try:
        entries = source.pull()
    except KeepAuthError as err:
        print(f"  Problem: {err}")
        return False
    except Exception as err:  # noqa: BLE001
        print(f"  Problem talking to Google Keep: {type(err).__name__}: {err}")
        return False
    lists = source.matching_lists()
    if not lists:
        wanted = " or ".join(repr(t) for t in titles)
        print(
            f"  Connected, but there is no Keep checklist called {wanted} yet.\n"
            '  Say "Hey Google, add test to my shopping list" and it will be created.'
        )
        return True
    for note in lists:
        print(f"  Found Keep list {note.title!r} with {len(note.unchecked)} unchecked item(s).")
    if entries:
        preview = ", ".join(e.text for e in entries[:8])
        print(f"  These will be moved to Bring! when the connector runs: {preview}")
    return True


async def _verify_bring(email: str, password: str, list_name: str | None) -> BringSink:
    async with _session() as session:
        sink = BringSink(email, password, list_name, session)
        await sink.connect()
        return sink


def _account_slots(values: dict[str, str]) -> list[tuple[str, str]]:
    """(suffix, email) for every configured Google account in an env mapping."""
    slots = []
    for index in range(1, MAX_GOOGLE_ACCOUNTS + 1):
        suffix = "" if index == 1 else f"_{index}"
        if values.get(f"GOOGLE_EMAIL{suffix}"):
            slots.append((suffix, values[f"GOOGLE_EMAIL{suffix}"]))
    return slots


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    env_file = resolve_env_file(args.env_file)
    existing = parse_env_file(env_file) if env_file.is_file() else {}
    print(
        f"Keep-to-Bring setup {__version__}\n\n"
        "This connects your Google Home voice shopping list (which Google now keeps in\n"
        "Google Keep) to your Bring! list. You'll need:\n"
        "  - your Bring! email and password\n"
        "  - to sign in to each Google account once in a web browser\n"
        f"\nSettings will be saved to: {env_file.resolve()}"
    )
    if existing and not _yes("A configuration already exists there. Update it?"):
        return 0

    # --- Bring! ---------------------------------------------------------
    _heading("Step 1 of 3: Bring!")
    print(
        "If you normally sign in to Bring! with Google or Apple, first set a password in\n"
        "the Bring! app: Profile > Settings > Change password (or use 'Forgot password'\n"
        "on the Bring! login screen). You can keep signing in with Google in the app."
    )
    bring_email = existing.get("BRING_EMAIL")
    while True:
        bring_email = _ask("Bring! email", bring_email)
        bring_password = _ask("Bring! password (hidden while typing)", secret=True)
        try:
            sink = asyncio.run(_verify_bring(bring_email, bring_password, None))
        except (BringAuthError, BringListNotFound) as err:
            print(f"\n  Problem: {err}\n")
        except Exception as err:  # noqa: BLE001
            print(f"\n  Could not reach Bring!: {type(err).__name__}: {err}\n")
        else:
            break
        if not _yes("Try again?"):
            return 1
    print(f"  Logged in to Bring!. Your default list is {sink.list_name!r}.")
    bring_list = sink.list_name
    if len(sink.lists) > 1:
        print("  Your Bring! lists:")
        for number, info in enumerate(sink.lists, 1):
            print(f"    {number}. {info.name}")
        default_number = next(
            (str(n) for n, info in enumerate(sink.lists, 1) if info.name == sink.list_name), "1"
        )
        while True:
            choice = _ask("Which list should voice items go to? Enter the number", default_number)
            if choice.isdigit() and 1 <= int(choice) <= len(sink.lists):
                bring_list = sink.lists[int(choice) - 1].name
                break
            print("  Please enter one of the numbers above.")

    # --- Keep list name -------------------------------------------------
    _heading("Step 2 of 3: Google Keep list name")
    print(
        'When you say "Hey Google, add milk to my shopping list", Google puts it on a\n'
        'Google Keep checklist called "Shopping list". Change this only if yours is\n'
        "named differently (separate several names with commas)."
    )
    titles_raw = _ask("Keep list name", existing.get("KEEP_LIST_TITLES") or "Shopping list")
    titles = [t.strip() for t in titles_raw.split(",") if t.strip()]

    # --- Google accounts ------------------------------------------------
    _heading("Step 3 of 3: Google account(s)")
    print(
        "Add the Google account your Google Home uses for the shopping list. If you and\n"
        "your partner each have Voice Match set up, each person's voice goes to their\n"
        "OWN Keep list - add both accounts so both get copied to Bring!."
    )
    accounts: list[GoogleAccount] = []
    previous = dict(_account_slots(existing))
    while len(accounts) < MAX_GOOGLE_ACCOUNTS:
        number = len(accounts) + 1
        suffix = "" if number == 1 else f"_{number}"
        email = _ask(f"Google account #{number} email", previous.get(suffix))
        old_token = existing.get(f"GOOGLE_MASTER_TOKEN{suffix}") if previous.get(suffix) == email else None
        if old_token and _yes("  A saved login exists for this account. Keep using it?"):
            token = old_token
        else:
            token = _obtain_master_token(email)
        account = GoogleAccount(email, token, default_device_id(email))
        if not _verify_keep(account, titles) and not _yes("Save this account anyway?", False):
            continue
        accounts.append(account)
        if not _yes("Add another Google account (for example your partner's)?", False):
            break

    # --- Save -----------------------------------------------------------
    updates: dict[str, str | None] = {
        "BRING_EMAIL": bring_email,
        "BRING_PASSWORD": bring_password,
        "BRING_LIST_NAME": bring_list,
        "KEEP_LIST_TITLES": ", ".join(titles),
    }
    for index in range(1, MAX_GOOGLE_ACCOUNTS + 1):
        suffix = "" if index == 1 else f"_{index}"
        account = accounts[index - 1] if index <= len(accounts) else None
        updates[f"GOOGLE_EMAIL{suffix}"] = account.email if account else None
        updates[f"GOOGLE_MASTER_TOKEN{suffix}"] = account.master_token if account else None
    write_env_file(env_file, updates, header=ENV_HEADER)

    _heading("Done")
    print(
        f"Saved to {env_file.resolve()} (keep this file private).\n\n"
        "Next steps:\n"
        "  1. Test it:   keep-to-bring check\n"
        "  2. Start it:  keep-to-bring run        (or: docker compose up -d)\n"
        '  3. Say "Hey Google, add bananas to my shopping list" and watch it\n'
        "     appear in Bring! within about a minute."
    )
    return 0


def cmd_get_token(args: argparse.Namespace) -> int:
    env_file = resolve_env_file(args.env_file)
    existing = parse_env_file(env_file) if env_file.is_file() else {}
    slots = _account_slots(existing)
    email = args.email or _ask("Google account email", slots[0][1] if len(slots) == 1 else None)
    token = _obtain_master_token(email)
    titles = [
        t.strip() for t in (existing.get("KEEP_LIST_TITLES") or "Shopping list").split(",") if t.strip()
    ]
    _verify_keep(GoogleAccount(email, token, default_device_id(email)), titles)

    if args.print_only:
        print(f"\nGOOGLE_MASTER_TOKEN={token}\n(Keep this secret - it works like a password.)")
        return 0

    suffix = next((s for s, e in slots if e.strip().lower() == email.strip().lower()), None)
    if suffix is None:
        used = {s for s, _ in slots}
        suffix = next(
            ("" if i == 1 else f"_{i}" for i in range(1, MAX_GOOGLE_ACCOUNTS + 1)
             if ("" if i == 1 else f"_{i}") not in used),
            None,
        )
        if suffix is None:
            print("Too many Google accounts configured.", file=sys.stderr)
            return 1
    write_env_file(
        env_file,
        {f"GOOGLE_EMAIL{suffix}": email, f"GOOGLE_MASTER_TOKEN{suffix}": token},
        header=ENV_HEADER,
    )
    print(
        f"\nSaved the new login for {email} to {env_file.resolve()}.\n"
        "If the connector is already running it will notice the new login by itself\n"
        "within a minute or two - no restart needed."
    )
    return 0


async def _check(settings: Settings) -> int:
    ok = True
    print(f"Keep list name(s): {', '.join(settings.keep_list_titles)}")
    print(f"After copying, items are {'deleted from' if settings.keep_action == 'delete' else 'checked off in'} Keep.")
    async with _session() as session:
        sink = BringSink(settings.bring_email, settings.bring_password, settings.bring_list_name, session)
        try:
            await sink.connect()
        except (BringAuthError, BringListNotFound) as err:
            print(f"[FAIL] Bring!: {err}")
            return 1
        print(f"[ OK ] Bring!: logged in as {settings.bring_email}, target list {sink.list_name!r}")
        connector = _connector(settings, sink)
        for source in connector.sources:
            try:
                entries = await run_blocking(source.pull)
            except KeepAuthError as err:
                ok = False
                print(f"[FAIL] Google Keep ({source.email}): {err}")
                continue
            except Exception as err:  # noqa: BLE001
                ok = False
                print(f"[FAIL] Google Keep ({source.email}): {type(err).__name__}: {err}")
                continue
            lists = source.matching_lists()
            if not lists:
                print(f"[WARN] Google Keep ({source.email}): connected, but no matching list found yet")
                continue
            names = ", ".join(repr(n.title) for n in lists)
            print(f"[ OK ] Google Keep ({source.email}): found {names}")
            for entry in entries:
                items = ", ".join(i.label() for i in connector.itemize(entry.text))
                print(f"         {entry.text!r} -> Bring!: {items}")
            if not entries:
                print("         (list is empty - nothing waiting to be copied)")
    print("\nEverything looks good." if ok else "\nSome checks failed - see above.")
    print("(Nothing was changed by this check.)")
    return 0 if ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    settings = _load(args)
    return asyncio.run(_check(settings))


async def _once(settings: Settings) -> int:
    async with _session() as session:
        sink = BringSink(settings.bring_email, settings.bring_password, settings.bring_list_name, session)
        connector = _connector(settings, sink)
        try:
            report = await connector.sync_once()
        except (BringAuthError, BringListNotFound) as err:
            _LOGGER.error("%s", err)
            return 1
        report.log()
        if report.healthy:
            write_heartbeat(settings.heartbeat_file, report)
            if not report.changed:
                _LOGGER.info("Nothing new on the Google Keep list.")
            return 0
        for email, error in report.source_errors.items():
            _LOGGER.error("%s: %s", email, error)
        return 1


def cmd_once(args: argparse.Namespace) -> int:
    settings = _load(args)
    return asyncio.run(_once(settings))


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass
    accounts = ", ".join(a.email for a in settings.google_accounts)
    _LOGGER.info(
        "Keep-to-Bring %s starting: Google Keep (%s) -> Bring! every %gs",
        __version__,
        accounts,
        settings.poll_interval,
    )
    async with _session() as session:
        sink = BringSink(settings.bring_email, settings.bring_password, settings.bring_list_name, session)
        connector = _connector(settings, sink, args)
        await run_forever(
            connector,
            interval=settings.poll_interval,
            heartbeat_file=settings.heartbeat_file,
            stop=stop,
        )
    _LOGGER.info("Stopped.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = _load(args)
    try:
        return asyncio.run(_run(settings, args))
    except KeyboardInterrupt:  # pragma: no cover - Windows Ctrl+C
        return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Healthy if a fully successful sync happened recently (used by Docker)."""
    env = load_environment(resolve_env_file(args.env_file))
    try:
        interval = float(env.get("POLL_INTERVAL_SECONDS") or DEFAULT_POLL_INTERVAL)
    except ValueError:
        interval = DEFAULT_POLL_INTERVAL
    heartbeat = Path(env.get("DATA_DIR") or "data") / "heartbeat"
    max_age = max(interval * 5, 300.0)
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except OSError:
        print("unhealthy: no successful sync yet")
        return 1
    if age > max_age:
        print(f"unhealthy: last successful sync {age:.0f}s ago")
        return 1
    print(f"healthy: last successful sync {age:.0f}s ago")
    return 0


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keep-to-bring",
        description="Copy Google Home voice shopping-list items (Google Keep) to Bring!.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        help="settings file to use (default: $KEEP_TO_BRING_ENV_FILE or ./.env)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("setup", help="interactive first-time setup (creates the settings file)").set_defaults(func=cmd_setup)
    token = sub.add_parser("get-token", help="create a new Google login token (e.g. after it stopped working)")
    token.add_argument("--email", help="Google account email")
    token.add_argument("--print", dest="print_only", action="store_true", help="print the token instead of saving it")
    token.set_defaults(func=cmd_get_token)
    sub.add_parser("check", help="test the settings without changing anything").set_defaults(func=cmd_check)
    sub.add_parser("once", help="copy new items once, then exit").set_defaults(func=cmd_once)
    sub.add_parser("run", help="keep copying new items until stopped").set_defaults(func=cmd_run)
    sub.add_parser("health", help="exit 0 if the last sync succeeded recently").set_defaults(func=cmd_health)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
