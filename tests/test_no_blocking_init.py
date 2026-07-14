"""Tests for the static no-loop-I/O guard (``scripts/check_no_loop_io.py``).

The guard is the automated tripwire for the class of bug that hung Home
Assistant at startup: a synchronous device read on the event loop inside an
entity ``__init__`` (or a ``@property`` getter). These tests prove the guard
(1) FLAGS a bad source string that reads ``self.data.<x>`` in ``__init__`` and
(2) PASSES on the real, fixed integration package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT = REPO_ROOT / "custom_components" / "rocket_r60v"
CHECKER_PATH = REPO_ROOT / "scripts" / "check_no_loop_io.py"


def _load_checker():
    """Import the stdlib-only checker module directly from ``scripts/``."""
    spec = importlib.util.spec_from_file_location("check_no_loop_io", CHECKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_loop_io"] = module
    spec.loader.exec_module(module)
    return module


check_no_loop_io = _load_checker()


# A constructor that reads live device state on the loop -- the original bug.
BAD_SOURCE = '''
class BadSwitch:
    def __init__(self, data, entry):
        self.data = data[entry.entry_id]
        self._attr_is_on = self.data.standby == "on"
'''

# The correct shape: bind the device, default the dynamic attr, fetch in update.
GOOD_SOURCE = '''
class GoodSwitch:
    def __init__(self, data, entry):
        self.data = data[entry.entry_id]
        self._attr_is_on = None

    def update(self):
        self._attr_is_on = self.data.standby == "on"
'''

# A blocking transport *call* on the device object inside __init__.
BAD_CALL_SOURCE = '''
class BadConnect:
    def __init__(self, data, entry):
        self.data = data[entry.entry_id]
        self.data.connect()
'''

# A property getter that reads the device -- also runs on the loop.
BAD_PROPERTY_SOURCE = '''
class BadProperty:
    def __init__(self, data, entry):
        self.data = data[entry.entry_id]

    @property
    def native_value(self):
        return self.data.current_brew_time
'''


def test_flags_blocking_read_in_init(tmp_path):
    path = tmp_path / "bad.py"
    path.write_text(BAD_SOURCE)
    violations = check_no_loop_io.check_source(BAD_SOURCE, path)
    assert violations, "checker failed to flag self.data read in __init__"
    assert any(attr == "standby" for _, attr in violations)

    messages = check_no_loop_io.check_path(path)
    assert messages and any("standby" in m for m in messages)


def test_flags_blocking_call_in_init(tmp_path):
    path = tmp_path / "bad_call.py"
    path.write_text(BAD_CALL_SOURCE)
    assert check_no_loop_io.check_source(BAD_CALL_SOURCE, path)


def test_flags_read_in_property_getter(tmp_path):
    path = tmp_path / "bad_prop.py"
    path.write_text(BAD_PROPERTY_SOURCE)
    violations = check_no_loop_io.check_source(BAD_PROPERTY_SOURCE, path)
    assert any(attr == "current_brew_time" for _, attr in violations)


def test_passes_clean_source(tmp_path):
    path = tmp_path / "good.py"
    path.write_text(GOOD_SOURCE)
    assert check_no_loop_io.check_source(GOOD_SOURCE, path) == []


def test_ignores_device_binding(tmp_path):
    """``self.data = data[...]`` is a binding, not a device read; don't flag it."""
    source = (
        "class Bind:\n"
        "    def __init__(self, data, entry):\n"
        "        self.data = data[entry.entry_id]\n"
    )
    path = tmp_path / "bind.py"
    path.write_text(source)
    assert check_no_loop_io.check_source(source, path) == []


def test_passes_on_real_integration():
    files = sorted(COMPONENT.glob("*.py"))
    assert files, "no integration source files found"
    for file in files:
        assert check_no_loop_io.check_path(file) == [], (
            f"unexpected blocking-I/O violation in {file}"
        )
