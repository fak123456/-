"""Auto-exit the process when the user closes every browser tab.

Mechanism
---------
The browser side runs a tiny snippet (see ``HEARTBEAT_JS``) that GETs
``/_gui_heartbeat`` every few seconds while at least one tab is open.
A custom ASGI middleware short-circuits that path *before* Gradio sees
it, refreshes a timestamp, and returns ``204 No Content``. A daemon
thread polls the timestamp: once the page has heartbeat-ed at least
once but then stops for more than ``missing_threshold`` seconds (i.e.
all tabs were closed and the JS interval stopped), the process exits.

Why not use ``beforeunload`` + ``navigator.sendBeacon`` for an instant
exit? Because that also fires on F5 reload and on tab navigation,
which would kill the app the moment the user wants to refresh. The
heartbeat-timeout approach handles reload naturally: a fresh page
resumes heartbeats well within the threshold.

We deliberately *do not* exit while a generation is still running
(``_BUSY.is_active``), so a user who accidentally closes the tab during
a batch run doesn't lose the in-flight image. The next watchdog tick
after generation finishes will pick the exit back up.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

HEARTBEAT_PATH = "/_gui_heartbeat"

# JS injected into the Gradio page. Runs once per Blocks load() and starts
# a 5-second interval that pings the heartbeat endpoint. Stays trivially
# small so we don't introduce an extra dependency or fragile lifecycle.
HEARTBEAT_JS = """
() => {
  if (window.__gui_hb_started) { return; }
  window.__gui_hb_started = true;
  const ping = () => {
    fetch('%s', {method: 'GET', cache: 'no-store', keepalive: true})
      .catch(() => {});
  };
  ping();
  window.__gui_hb_timer = setInterval(ping, 5000);
}
""" % HEARTBEAT_PATH

_state = {
    "last_beat": 0.0,
    "ever_connected": False,
    "lock": threading.Lock(),
}


def _touch_heartbeat() -> None:
    with _state["lock"]:
        _state["last_beat"] = time.monotonic()
        _state["ever_connected"] = True


class BrowserHeartbeatMiddleware:
    """Intercepts ``/_gui_heartbeat`` and short-circuits with a 204 reply.

    Sits in front of the Gradio app so the heartbeat URL never goes through
    Gradio's queue / FastAPI routing; that makes it safe even while the
    user is mid-generation (where the queue is busy).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == HEARTBEAT_PATH:
            _touch_heartbeat()
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def browser_heartbeat_middleware() -> Middleware:
    """Starlette ``Middleware`` factory, pass via Gradio's ``app_kwargs``."""
    return Middleware(BrowserHeartbeatMiddleware)


def start_browser_watchdog(
    *,
    missing_threshold: float = 20.0,
    tick_seconds: float = 3.0,
    is_busy: Callable[[], bool] | None = None,
    verbose: bool = False,
) -> None:
    """Spawn a daemon thread that exits when the browser has been gone too long.

    ``missing_threshold`` (s): how long without a heartbeat before we shut down.
    Default of 20 s = ~4 missed pings, well above any normal F5 reload.

    ``is_busy``: optional callable returning ``True`` while a generation is
    still running. If provided, the watchdog will *not* exit while busy.
    """

    def _watch() -> None:
        # Wait for the first heartbeat before arming, otherwise the exe would
        # exit before the browser has had a chance to open.
        while True:
            time.sleep(tick_seconds)
            with _state["lock"]:
                ever = _state["ever_connected"]
                last = _state["last_beat"]
            if not ever:
                continue
            idle = time.monotonic() - last
            if verbose:
                print(
                    f"[browser-watchdog] last_beat={idle:.1f}s ago, "
                    f"threshold={missing_threshold}s, "
                    f"busy={'?' if is_busy is None else is_busy()}",
                    flush=True,
                )
            if idle <= missing_threshold:
                continue
            if is_busy is not None and is_busy():
                # Generation is still running. Don't yank the rug; wait for
                # the next tick — if the browser is genuinely gone the worker
                # will finish and then we'll exit on the following pass.
                continue
            if verbose:
                print("[browser-watchdog] browser gone, exiting", flush=True)
            os._exit(0)

    threading.Thread(
        target=_watch, daemon=True, name="browser-watchdog"
    ).start()
