"""Network safety for the blocking Google libraries.

``gkeepapi`` and ``gpsoauth`` use ``requests`` without any timeout, so a
half-open connection (e.g. after a router hiccup) would block forever and
freeze the whole service. These helpers make sure that can't happen.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import requests

# (connect, read) seconds for every requests call that doesn't set its own.
REQUEST_TIMEOUT: tuple[float, float] = (15.0, 60.0)

_T = TypeVar("_T")


def install_request_timeouts() -> None:
    """Give every ``requests`` call a timeout unless the caller passed one."""
    current = requests.Session.request
    if getattr(current, "_keep_to_bring_timeout", False):
        return

    def request(self, method, url, *args, **kwargs):  # noqa: ANN001, ANN202
        # ``timeout`` is the 7th positional parameter of Session.request.
        if len(args) < 7 and kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT
        return current(self, method, url, *args, **kwargs)

    request._keep_to_bring_timeout = True  # type: ignore[attr-defined]
    requests.Session.request = request  # type: ignore[method-assign]


async def run_blocking(func: Callable[..., _T], *args: Any) -> _T:
    """Run a blocking call in a daemon thread and await its result.

    Unlike ``asyncio.to_thread`` the thread is never joined at shutdown, so a
    slow Google request can't stop the service from exiting on SIGTERM or
    Ctrl+C. (Stopping mid-sync is safe: items are only removed from Keep
    after Bring! has them, and a retry skips items already on Bring!.)
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def worker() -> None:
        try:
            result = func(*args)
        except BaseException as err:  # noqa: BLE001 - re-raised in the awaiting task
            outcome: tuple[Callable[[Any], None], Any] = (future.set_exception, err)
        else:
            outcome = (future.set_result, result)
        try:
            loop.call_soon_threadsafe(deliver, *outcome)
        except RuntimeError:  # event loop already closed (shutting down)
            pass

    name = f"keep-to-bring-{getattr(func, '__name__', 'worker')}"
    threading.Thread(target=worker, name=name, daemon=True).start()
    return await future
