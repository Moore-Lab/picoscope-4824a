"""Enforce the promise that the CLI, the HTTP API and the GUI stay equivalent.

The repo's central claim is that anything a person can do at the GUI, a program
can do from the command line — because all three surfaces are rendered from
:mod:`picoscope_4824a.registry` rather than written three times. A claim like
that decays silently unless something checks it, so these tests walk the
registry and fail the build if any surface has fallen behind.

None of this needs a scope attached.

Run::

    python -m pytest tests/ -q
    python tests/test_parity.py          # also works without pytest
"""

from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a import registry                      # noqa: E402
from picoscope_4824a.controller import ScopeController    # noqa: E402


def test_every_command_has_a_handler():
    """Each registry entry names a real ScopeController method."""
    missing = [c.name for c in registry.COMMANDS
               if not callable(getattr(ScopeController, c.handler, None))]
    assert not missing, f"commands with no controller method: {missing}"


def test_every_handler_accepts_its_declared_parameters():
    """A declared parameter the handler cannot accept would fail only at runtime.

    This is the check that actually catches drift: rename a controller
    argument and the CLI keeps offering the old flag until someone tries it.
    """
    problems = []
    for cmd in registry.COMMANDS:
        fn = getattr(ScopeController, cmd.handler, None)
        if fn is None:
            continue
        sig = inspect.signature(fn)
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in sig.parameters.values())
        if accepts_kwargs:
            continue
        for param in cmd.params:
            if param.name not in sig.parameters:
                problems.append(f"{cmd.name}: handler {cmd.handler}() has no "
                                f"argument '{param.name}'")
    assert not problems, "registry/handler mismatch:\n  " + "\n  ".join(problems)


def test_every_command_is_a_cli_subcommand():
    """The CLI parser exposes every command, with every declared option."""
    from picoscope_4824a.cli import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions
                  if isinstance(a, __import__("argparse")._SubParsersAction)]
    assert subparsers, "the CLI has no subcommands"
    choices = subparsers[0].choices

    missing = [c.name for c in registry.COMMANDS if c.name not in choices]
    assert not missing, f"commands absent from the CLI: {missing}"

    problems = []
    for cmd in registry.COMMANDS:
        sub = choices[cmd.name]
        flags = {opt for action in sub._actions for opt in action.option_strings}
        for param in cmd.params:
            flag = "--" + param.name.replace("_", "-")
            if flag not in flags:
                problems.append(f"{cmd.name} is missing {flag}")
    assert not problems, "CLI options missing:\n  " + "\n  ".join(problems)


def test_every_command_is_an_http_route():
    """The web app exposes every command at its registry route and method."""
    from picoscope_4824a.webapp import create_app

    app = create_app(manage_lifecycle=False)
    routes = {(r.path, method)
              for r in app.routes
              for method in getattr(r, "methods", set()) or set()}

    problems = []
    for cmd in registry.COMMANDS:
        want = (cmd.route, "POST" if cmd.mutates else "GET")
        if want not in routes:
            problems.append(f"{cmd.name}: no {want[1]} {want[0]}")
    assert not problems, "HTTP routes missing:\n  " + "\n  ".join(problems)


def test_describe_is_complete_and_serialisable():
    """``describe`` is the discovery entry point — it must be complete JSON."""
    import json

    doc = registry.describe()
    assert doc["commands"], "describe() returned no commands"
    assert len(doc["commands"]) == len(registry.COMMANDS)

    for entry in doc["commands"]:
        for key in ("name", "group", "summary", "route", "method", "params"):
            assert key in entry, f"{entry.get('name')}: describe() lost '{key}'"
        assert entry["group"] in doc["groups"], \
            f"{entry['name']} is in undeclared group {entry['group']!r}"

    json.dumps(doc)          # must round-trip: an agent parses this


def test_command_names_are_unique():
    names = [c.name for c in registry.COMMANDS]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate command names: {dupes}"


def test_mutating_commands_are_marked():
    """Anything that changes state must be POST, so a GET is always safe.

    An agent should be able to poll every read-only command freely without
    wondering whether it just re-armed the trigger.
    """
    should_mutate = ("open", "close", "channel-set", "trigger-set", "capture",
                     "stream-start", "stream-stop", "record-start",
                     "record-stop", "siggen", "siggen-off", "preset-save",
                     "preset-load", "flash")
    for name in should_mutate:
        cmd = registry.by_name(name)
        assert cmd is not None, f"{name} is not in the registry"
        assert cmd.mutates, f"{name} changes state but is not marked mutating"

    read_only = ("status", "info", "channels", "stream-status", "measure",
                 "recordings", "presets", "stream-plan", "list")
    for name in read_only:
        cmd = registry.by_name(name)
        assert cmd is not None, f"{name} is not in the registry"
        assert not cmd.mutates, f"{name} is read-only but marked mutating"


def test_controller_never_raises_on_a_closed_scope():
    """Every read-only command returns a result dict with no device attached.

    The contract an agent relies on: a command always answers, even when the
    hardware is absent, and says why in a field it can branch on.
    """
    controller = ScopeController()
    for cmd in registry.COMMANDS:
        if cmd.mutates:
            continue
        handler = getattr(controller, cmd.handler)
        kwargs = {p.name: p.default for p in cmd.params
                  if p.default is not None}
        result = handler(**kwargs)
        assert isinstance(result, dict), f"{cmd.name} did not return a dict"
        assert "ok" in result, f"{cmd.name} result has no 'ok' field"
        if not result["ok"]:
            assert result.get("error"), f"{cmd.name} failed without an error"
            assert result.get("error_type"), \
                f"{cmd.name} failed without an error_type"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n        {exc}")
        except Exception as exc:                       # noqa: BLE001
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'all parity checks passed'}"
          f"{f' ({failures})' if failures else ''}")
    sys.exit(1 if failures else 0)
