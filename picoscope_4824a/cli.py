"""Command-line interface — every capability the GUI has, driveable from a shell.

The CLI is not a reduced convenience wrapper. It is generated from
:mod:`picoscope_4824a.registry`, the same table the web UI builds its forms
from, so anything you can do by clicking you can do by typing, and neither
surface can quietly gain a feature the other lacks.

Designed to be driven by a program as readily as by a person:

* ``--json`` on any command prints one JSON document on stdout and nothing else.
* ``pico describe --json`` dumps the entire command tree — every command, every
  parameter, type, default and choice. One call is enough to discover the whole
  instrument.
* Exit codes are meaningful: ``0`` success, ``1`` the command ran and failed,
  ``2`` bad usage, ``3`` no scope available.
* Errors are structured, on stdout with ``--json``, so a caller never has to
  parse prose.

**The session problem.** A PicoScope is an exclusive USB device that must be
opened and configured before it can capture, and opening costs a moment. A CLI
that opened and closed the device per invocation could never hold a stream
running between two commands. So the device lives in a **server** — the same
FastAPI app that the DAQ panel mounts — and the CLI is a thin client to it::

    pico serve --port 8100 &          # holds the scope open
    pico open --json
    pico stream-start --rate 1e6 --downsample 1000 --mode AGGREGATE
    pico stream-status --json         # the stream is still running

With no server running, commands fall back to a one-shot local session (open,
run, close), which is fine for ``info`` or ``capture`` but cannot hold a stream.
``--local`` forces that mode; ``--url`` forces the client mode.

This means the agent and the human hit *identical code paths*: the browser
posts to ``/api/stream-start`` and so does the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from . import registry
from .registry import COMMANDS, Command, Param

DEFAULT_URL = os.environ.get("PICOSCOPE_URL", "http://127.0.0.1:8100")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_SCOPE = 3


# --------------------------------------------------------------------------
# parser construction
# --------------------------------------------------------------------------

def _add_param(parser: argparse.ArgumentParser, param: Param) -> None:
    """Render one registry parameter as an argparse option."""
    flag = "--" + param.name.replace("_", "-")
    help_text = param.help
    if param.unit:
        help_text = f"{help_text} [{param.unit}]".strip()
    if param.default is not None:
        help_text = f"{help_text} (default: {param.default})".strip()

    kwargs: dict[str, Any] = {"help": help_text, "dest": param.name,
                              "default": None}
    if param.type == "int":
        kwargs["type"] = int
    elif param.type == "float":
        kwargs["type"] = float
    elif param.type == "bool":
        # Explicit true/false rather than a bare flag: an agent generating a
        # command line should never have to know that absence means false.
        kwargs["type"] = _parse_bool
        kwargs["metavar"] = "true|false"
    elif param.type == "enum" and param.choices:
        kwargs["choices"] = list(param.choices)
    if param.required:
        kwargs["required"] = True
    parser.add_argument(flag, **kwargs)


def _parse_bool(value: str) -> bool:
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "on", "y", "t"):
        return True
    if lowered in ("0", "false", "no", "off", "n", "f"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI, rendered from the registry."""
    parser = argparse.ArgumentParser(
        prog="pico",
        description="Control a PicoScope 4824A. Every command accepts --json.",
        epilog="Run 'pico describe --json' for the full machine-readable "
               "command tree.")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON document and nothing else")
    parser.add_argument("--url", default=None,
                        help=f"server holding the scope (default: {DEFAULT_URL})")
    parser.add_argument("--local", action="store_true",
                        help="open the device for this command only, instead of "
                             "talking to a server (cannot hold a stream running)")
    parser.add_argument("--serial", default=None,
                        help="serial of the scope to use in --local mode")
    parser.add_argument("--sim", action="store_true",
                        help="run against a simulated scope — no hardware, no "
                             "driver. Implies --local.")

    subs = parser.add_subparsers(dest="command", metavar="<command>")

    def _add_global_flags(sub: argparse.ArgumentParser) -> None:
        """Repeat the global flags on each subcommand.

        ``pico describe --json`` is how anyone actually types it, but argparse
        only accepts a parent-level flag *before* the subcommand. Re-declaring
        them here with ``SUPPRESS`` defaults means either position works and
        the subparser never clobbers a value the parent already set.
        """
        sub.add_argument("--json", action="store_true", dest="json",
                         default=argparse.SUPPRESS,
                         help="emit one JSON document and nothing else")
        sub.add_argument("--url", dest="url", default=argparse.SUPPRESS,
                         help=f"server holding the scope (default: {DEFAULT_URL})")
        sub.add_argument("--local", action="store_true", dest="local",
                         default=argparse.SUPPRESS,
                         help="open the device for this command only")
        sub.add_argument("--sim", action="store_true", dest="sim",
                         default=argparse.SUPPRESS,
                         help="run against a simulated scope (implies --local)")

    # Grouped help: commands listed under their section headings.
    for group_key, group_label in registry.GROUPS.items():
        for cmd in COMMANDS:
            if cmd.group != group_key:
                continue
            sub = subs.add_parser(
                cmd.name, help=f"[{group_label}] {cmd.summary}",
                description=(cmd.summary + ("\n\n" + cmd.detail if cmd.detail else "")),
                formatter_class=argparse.RawDescriptionHelpFormatter)
            for param in cmd.params:
                _add_param(sub, param)
            _add_global_flags(sub)

    describe = subs.add_parser(
        "describe", help="[Meta] Dump the full command tree as JSON.",
        description="Print every command, parameter, type and default as one "
                    "JSON document — the discovery entry point for a program "
                    "driving this tool.")
    describe.add_argument("--group", default=None,
                          help="restrict to one group")
    _add_global_flags(describe)

    serve = subs.add_parser(
        "serve", help="[Meta] Run the server that holds the scope open.",
        description="Start the web app: a browser UI plus the HTTP API this "
                    "CLI talks to. This is the same app the xsphere-daq panel "
                    "mounts at /scope.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    serve.add_argument("--serial", default=None,
                       help="open this specific scope on startup")
    serve.add_argument("--no-open", action="store_true",
                       help="start without opening the scope")
    serve.add_argument("--sim", action="store_true",
                       help="serve a simulated scope (no hardware)")

    return parser


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def _server_alive(url: str, timeout: float = 0.6) -> bool:
    """Cheap probe: is a scope server answering at *url*?"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/ping",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _call_server(url: str, cmd: Command, params: dict) -> dict:
    """Invoke one command over HTTP."""
    import urllib.error
    import urllib.request

    endpoint = f"{url.rstrip('/')}{cmd.route}"
    if cmd.mutates:
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"})
    else:
        from urllib.parse import urlencode
        query = urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            endpoint + (f"?{query}" if query else ""), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}",
                    "error_type": "HTTPError"}
    except Exception as exc:
        return {"ok": False, "error": f"cannot reach {endpoint}: {exc}",
                "error_type": "ConnectionError"}


def _call_local(cmd: Command, params: dict, serial: Optional[str],
                simulate: bool = False) -> dict:
    """Run one command against a device opened just for this invocation."""
    from .controller import ScopeController

    controller = ScopeController(simulate=simulate)
    needs_device = cmd.name not in ("list", "stream-plan", "recordings",
                                    "presets", "channel-range-for")
    try:
        if needs_device:
            opened = controller.open(serial)
            if not opened.get("ok"):
                return opened
        handler = getattr(controller, cmd.handler)
        result = handler(**params)
        if cmd.group == "stream" and cmd.name == "stream-start" and result.get("ok"):
            result["warning"] = (
                "started in --local mode: the stream stops when this command "
                "exits. Run 'pico serve' and drop --local to keep it running.")
        return result
    finally:
        controller.shutdown()


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def _print_human(cmd: Optional[Command], result: dict) -> None:
    """Readable rendering for a person; --json is the machine path."""
    if not result.get("ok", False):
        print(f"error: {result.get('error', 'failed')}", file=sys.stderr)
        if result.get("status_name"):
            print(f"  driver status: {result['status_name']} "
                  f"({result.get('function', '')})", file=sys.stderr)
        return

    for warn in _warnings(result):
        print(f"warning: {warn}", file=sys.stderr)

    payload = {k: v for k, v in result.items()
               if k not in ("ok", "warning", "warnings")}
    if len(payload) == 1:
        payload = next(iter(payload.values()))
    _print_value(payload)


def _warnings(result: dict) -> list:
    out = []
    if result.get("warning"):
        out.append(result["warning"])
    out.extend(result.get("warnings") or [])
    for value in result.values():
        if isinstance(value, dict):
            if value.get("warning"):
                out.append(value["warning"])
            out.extend(value.get("warnings") or [])
    return out


def _print_value(value: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                print(f"{pad}{key}:")
                _print_value(item, indent + 1)
            else:
                print(f"{pad}{key}: {_fmt(item)}")
    elif isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            for item in value:
                _print_value(item, indent)
                print()
        else:
            preview = value if len(value) <= 12 else value[:12] + ["..."]
            print(f"{pad}{', '.join(_fmt(v) for v in preview)}")
    else:
        print(f"{pad}{_fmt(value)}")


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.6g}"
    if value is None:
        return "-"
    return str(value)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    # --- meta commands ---
    if args.command == "describe":
        doc = registry.describe()
        if args.group:
            doc["commands"] = [c for c in doc["commands"]
                               if c["group"] == args.group]
        print(json.dumps(doc, indent=2))
        return EXIT_OK

    if args.command == "serve":
        from .webapp import serve
        serve(host=args.host, port=args.port, serial=args.serial,
              open_on_start=not args.no_open, simulate=args.sim)
        return EXIT_OK

    cmd = registry.by_name(args.command)
    if cmd is None:
        print(f"unknown command: {args.command}", file=sys.stderr)
        return EXIT_USAGE

    # Only parameters the user actually supplied, so the controller's own
    # defaults stay authoritative rather than being shadowed by argparse.
    params = {p.name: getattr(args, p.name) for p in cmd.params
              if getattr(args, p.name, None) is not None}

    url = args.url or DEFAULT_URL
    simulate = getattr(args, "sim", False)
    if simulate or args.local:
        # A simulated scope only exists inside this process, so --sim can never
        # be satisfied by a server holding real hardware.
        result = _call_local(cmd, params, args.serial, simulate=simulate)
    elif args.url or _server_alive(url):
        result = _call_server(url, cmd, params)
    else:
        result = _call_local(cmd, params, args.serial)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(cmd, result)

    if result.get("ok"):
        return EXIT_OK
    if result.get("status_name") in ("PICO_NOT_FOUND", "PICO_INVALID_HANDLE"):
        return EXIT_NO_SCOPE
    if "not open" in str(result.get("error", "")):
        return EXIT_NO_SCOPE
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
