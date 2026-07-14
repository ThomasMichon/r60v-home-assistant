#!/usr/bin/env python3
"""Static guard: no device I/O in entity ``__init__`` or ``@property`` getters.

Home Assistant constructs entities and evaluates their property getters on the
event loop. The Rocket R60V entities share one device object exposed as
``self.data`` (a ``LockedMachine``); reading any attribute off it performs a
*synchronous, blocking* socket round-trip to the machine/bridge. Doing that on
the event loop hangs Home Assistant at startup -- the exact failure this repo
was bitten by. Dynamic state must instead be fetched in the entity's
``update()`` method, which HA runs in an executor (off the loop).

This checker walks every module under ``custom_components/rocket_r60v`` and
flags, with a nonzero exit and ``file:line`` locations, any *read* of an
attribute on the shared device object (``self.data.<attr>``) that happens inside
an ``__init__`` method or a ``@property`` getter.

It is deliberately conservative to avoid false positives:

* Only ``self.data.<attr>`` reads are flagged. The binding assignment
  ``self.data = data[entry.entry_id]`` is a *write* to ``self.data`` (not a read
  of an attribute *on* it) and is ignored.
* Assigning a static value to a ``self._attr_*`` attribute is fine; only a
  read of ``self.data.<attr>`` is flagged.
* Method calls such as ``self.data.connect()`` are likewise flagged, since they
  are blocking transport calls on the shared device object.

Stdlib-only; no third-party dependencies.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# The attribute name on an entity that holds the shared blocking device object.
DEVICE_ATTR = "data"


def _is_self_data(node: ast.AST) -> bool:
    """True if ``node`` is the expression ``self.data``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == DEVICE_ATTR
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_self_data_read(node: ast.AST) -> bool:
    """True if ``node`` is a read on the device object: ``self.data.<x>``.

    Matches attribute reads (``self.data.standby``) and method calls
    (``self.data.connect()`` resolves to an ``Attribute`` whose value is
    ``self.data``). Does *not* match the binding ``self.data`` itself.
    """
    return isinstance(node, ast.Attribute) and _is_self_data(node.value)


def _is_property_getter(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function is decorated with a bare ``@property``."""
    for dec in func.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "property":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "property":
            return True
    return False


class _GuardedScopeVisitor(ast.NodeVisitor):
    """Within a guarded function body, record every ``self.data.<x>`` read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_self_data_read(node):
            self.violations.append((node.lineno, node.attr))
        self.generic_visit(node)


class _ModuleVisitor(ast.NodeVisitor):
    """Find guarded scopes (``__init__`` / property getters) and scan them."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[tuple[int, str]] = []

    def _scan_guarded(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        scope = _GuardedScopeVisitor(self.path)
        for stmt in func.body:
            scope.visit(stmt)
        self.violations.extend(scope.violations)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "__init__" or _is_property_getter(node):
            self._scan_guarded(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Async getters/inits are unusual here, but guard them the same way.
        if node.name == "__init__" or _is_property_getter(node):
            self._scan_guarded(node)
        self.generic_visit(node)


def check_source(source: str, path: Path) -> list[tuple[int, str]]:
    """Return a sorted list of ``(lineno, attr)`` violations for one source."""
    tree = ast.parse(source, filename=str(path))
    visitor = _ModuleVisitor(path)
    visitor.visit(tree)
    return sorted(visitor.violations)


def check_path(path: Path) -> list[str]:
    """Check one file; return formatted ``file:line`` violation messages."""
    source = path.read_text(encoding="utf-8")
    messages = []
    for lineno, attr in check_source(source, path):
        messages.append(
            f"{path}:{lineno}: blocking device read 'self.{DEVICE_ATTR}.{attr}' "
            f"inside __init__/property -- move it to update() (runs off the "
            f"event loop)"
        )
    return messages


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        targets = [Path(a) for a in argv]
    else:
        root = Path(__file__).resolve().parent.parent
        targets = sorted((root / "custom_components" / "rocket_r60v").glob("*.py"))

    all_messages: list[str] = []
    for target in targets:
        if target.is_dir():
            files = sorted(target.glob("*.py"))
        else:
            files = [target]
        for file in files:
            all_messages.extend(check_path(file))

    if all_messages:
        print("Blocking device I/O found in entity __init__/property getters:")
        for message in all_messages:
            print(f"  {message}")
        print(
            "\nHome Assistant runs __init__ and property getters on the event "
            "loop; reading self.data there blocks it. Fetch dynamic state in "
            "update() instead."
        )
        return 1

    print("OK: no blocking device I/O in entity __init__/property getters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
