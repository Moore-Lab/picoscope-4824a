"""FastAPI app — the browser UI, the HTTP API, and the xsphere-daq mount point.

One app serves three callers:

* a **browser** at ``/``, which is the development/testing GUI;
* the **CLI**, which posts to the same ``/api/*`` routes; and
* the **xsphere-daq panel**, which mounts this app at ``/scope``.

All three therefore exercise identical code. There is no separate "GUI logic":
the routes are generated from :mod:`picoscope_4824a.registry` and dispatch
straight to :class:`~picoscope_4824a.controller.ScopeController`.

Mounting into the panel
-----------------------
The panel's contract (see ``xsphere_daq/panel.py``) is:

* expose a module-level ``create_app(...)`` returning a FastAPI app;
* do **not** rely on the app's own lifespan — Starlette never runs a mounted
  sub-app's lifespan — and instead publish cleanup on
  ``app.state.shutdown_hooks``, which the panel calls on the way down;
* never emit root-absolute URLs, since the app lives under a prefix. The page
  derives its API base from ``window.location``, exactly as the camera dock
  and droplab pages do.

Pass ``manage_lifecycle=False`` when mounting so the panel owns opening and
closing the device, the same way it owns the cameras::

    from picoscope_4824a import webapp as scope_webapp
    scope_app = scope_webapp.create_app(manage_lifecycle=False)
    app.mount("/scope", scope_app)
    # and on shutdown: for hook in scope_app.state.shutdown_hooks: hook()

Shutting the scope down cleanly is load-bearing, not tidiness: a process that
dies mid-stream can leave a thread spinning inside ``ps4000a.dll`` that
survives ``taskkill /F`` and holds the device until it is physically
re-plugged. The shutdown hook stops the stream and closes the unit.

Live data
---------
The page polls JSON. The camera dock streams MJPEG because a camera produces
images; a scope produces a few thousand floats, and a poll at a handful of Hz
costs less than holding a streaming response open — and it degrades gracefully
when the browser tab is hidden. This matches how every non-video pane in the
panel already works.
"""

from __future__ import annotations

import os
from typing import Optional

from . import registry
from .controller import ScopeController
from .registry import COMMANDS

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(controller: Optional[ScopeController] = None,
               manage_lifecycle: bool = True,
               serial: Optional[str] = None,
               open_on_start: bool = False,
               simulate: bool = False):
    """Build the FastAPI app.

    :param controller: reuse an existing controller (the panel may own it);
        one is created if omitted.
    :param manage_lifecycle: when True the app opens the scope on startup and
        closes it on shutdown. Mount with ``False`` so the panel owns that.
    :param serial: open this specific unit rather than the first found.
    :param open_on_start: open the scope during startup.
    """
    from fastapi import Body, FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse

    ctrl = controller or ScopeController(simulate=simulate)

    app = FastAPI(title="PicoScope 4824A")
    app.state.controller = ctrl
    # The panel calls these on the way down; Starlette will not run our lifespan.
    app.state.shutdown_hooks = [ctrl.shutdown]

    if manage_lifecycle:
        @app.on_event("startup")
        def _startup() -> None:
            if open_on_start:
                ctrl.open(serial)

        @app.on_event("shutdown")
        def _shutdown() -> None:
            ctrl.shutdown()

    # --- pages ------------------------------------------------------------
    # Read from disk per request and never cached: a stale scope.js silently
    # reverts a fix and costs an hour debugging something already correct on
    # disk. Same policy as camera_dock/webapp.py `_page`.
    def _page(path: str):
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index():
        return _page(os.path.join(_STATIC, "index.html"))

    @app.get("/static/{fname}", include_in_schema=False)
    def static_file(fname: str):
        path = os.path.join(_STATIC, os.path.basename(fname))
        if not os.path.isfile(path):
            raise HTTPException(404)
        return _page(path)

    @app.get("/describe")
    @app.get("/api/describe")
    def describe():
        """The command tree — what the UI builds its forms from."""
        return registry.describe()

    # --- generated command routes ----------------------------------------
    # One route per registry entry. Written as a loop so a new capability is
    # a registry entry plus a controller method, never a hand-written route
    # that might disagree with the CLI.
    #
    # Both endpoints close over `cmd` and `handler` rather than carrying them
    # as default arguments. FastAPI reads a route function's signature to
    # decide what to bind, so a default argument holding the controller would
    # be treated as a request parameter — and deep-copying it explodes on the
    # ctypes pointers inside the driver handle. The GET signature is also set
    # *before* the route is registered, for the same reason: the decorator
    # would otherwise capture the original one.
    import inspect

    def _bind(cmd) -> None:
        handler = getattr(ctrl, cmd.handler, None)
        if handler is None:                     # caught by tests/test_parity.py
            return

        def _respond(params: dict):
            result = handler(**params)
            return JSONResponse(result,
                                status_code=200 if result.get("ok") else 400)

        if cmd.mutates:
            def endpoint(payload: dict = Body(default={})):
                return _respond(_coerce(cmd, payload or {}))
            methods = ["POST"]
        else:
            def endpoint(**query):
                return _respond(_coerce(
                    cmd, {k: v for k, v in query.items() if v is not None}))
            endpoint.__signature__ = inspect.Signature(
                [inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                   default=Query(default=None))
                 for p in cmd.params])
            methods = ["GET"]

        endpoint.__name__ = cmd.name.replace("-", "_")
        app.add_api_route(cmd.route, endpoint, methods=methods, name=cmd.name,
                          summary=cmd.summary,
                          description=cmd.detail or cmd.summary)

    for _cmd in COMMANDS:
        _bind(_cmd)

    return app


def _coerce(cmd, payload: dict) -> dict:
    """Cast incoming JSON/query values to the types the registry declares.

    A browser form and a query string both deliver strings; the controller
    wants numbers and bools. Unknown keys are dropped rather than passed on, so
    a stray field cannot become an unexpected keyword argument.
    """
    known = {p.name: p for p in cmd.params}
    out: dict = {}
    for key, value in payload.items():
        param = known.get(key)
        if param is None or value is None or value == "":
            continue
        try:
            if param.type == "int":
                out[key] = int(float(value))
            elif param.type == "float":
                out[key] = float(value)
            elif param.type == "bool":
                out[key] = (value if isinstance(value, bool)
                            else str(value).strip().lower()
                            in ("1", "true", "yes", "on", "y", "t"))
            else:
                out[key] = value
        except (TypeError, ValueError):
            out[key] = value                    # let the controller complain
    return out


def serve(host: str = "127.0.0.1", port: int = 8100,
          serial: Optional[str] = None, open_on_start: bool = True,
          simulate: bool = False) -> None:
    """Run the app standalone — ``pico serve``.

    Built as a ``uvicorn.Server`` rather than ``uvicorn.run()`` so shutdown is
    controllable, matching how the DAQ panel runs itself.
    """
    import uvicorn

    app = create_app(manage_lifecycle=True, serial=serial,
                     open_on_start=open_on_start, simulate=simulate)
    config = uvicorn.Config(app, host=host, port=port,
                            timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    app.state.server = server
    print(f"PicoScope 4824A panel on http://{host}:{port}/"
          + ("  [SIMULATED]" if simulate else ""))
    try:
        server.run()
    finally:
        # Belt and braces: uvicorn runs the shutdown event, but if it is killed
        # before that we still must not leave a stream running in the driver.
        for hook in getattr(app.state, "shutdown_hooks", []):
            try:
                hook()
            except Exception:
                pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the PicoScope 4824A web panel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--no-open", action="store_true",
                        help="start without opening the scope")
    parser.add_argument("--sim", action="store_true",
                        help="run against a simulated scope (no hardware)")
    args = parser.parse_args()
    serve(host=args.host, port=args.port, serial=args.serial,
          open_on_start=not args.no_open, simulate=args.sim)


if __name__ == "__main__":
    main()
