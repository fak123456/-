"""HTTP activity tracking + idle exit for frozen/local one-file runs.

We expose two pieces:
  * `IdleTrackerMiddleware` — a pure ASGI class that touches an activity
    timestamp on every HTTP / WebSocket scope.
  * `start_idle_watchdog(...)` — a daemon thread that calls `os._exit(0)` after
    the configured idle threshold (seconds) once at least one request has been
    served (so the exe does not exit before the browser ever opens it).

We deliberately install the middleware via Gradio's `app_kwargs={"middleware": [...]}`
plumbing instead of `@app.middleware("http")` after the fact, because FastAPI
builds its middleware stack the first time uvicorn calls the app, and a late
decorator on `demo.app` is silently dropped when the stack is already built.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

_touch_state = {"t": time.monotonic(), "served": False, "verbose": False}


def touch_activity() -> None:
    _touch_state["t"] = time.monotonic()
    _touch_state["served"] = True


def reset_activity_tracking() -> None:
    _touch_state["t"] = time.monotonic()
    _touch_state["served"] = False


def set_verbose(v: bool) -> None:
    _touch_state["verbose"] = bool(v)


class IdleTrackerMiddleware:
    """ASGI middleware that updates the last-activity timestamp on each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in ("http", "websocket"):
            touch_activity()
        await self.app(scope, receive, send)


def idle_middleware() -> Middleware:
    """Return a Starlette `Middleware` factory to pass via FastAPI's `middleware` kwarg."""
    return Middleware(IdleTrackerMiddleware)


def start_idle_watchdog(
    get_idle_seconds: Callable[[], int],
    *,
    tick_seconds: float = 15.0,
) -> None:
    """Spawn a daemon thread that exits the process after sustained idle time."""

    def _watch() -> None:
        while True:
            time.sleep(tick_seconds)
            try:
                idle = int(get_idle_seconds())
            except (TypeError, ValueError):
                idle = 600
            if idle < 60:
                idle = 60
            since = time.monotonic() - _touch_state["t"]
            if _touch_state.get("verbose"):
                print(
                    f"[idle-watchdog] served={_touch_state['served']} "
                    f"idle_threshold={idle}s since_last_request={since:.1f}s",
                    flush=True,
                )
            if _touch_state["served"] and since > float(idle):
                if _touch_state.get("verbose"):
                    print("[idle-watchdog] idle exceeded, exiting", flush=True)
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def exit_process_now() -> None:
    os._exit(0)
