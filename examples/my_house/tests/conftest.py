import sys
import types

import pytest


@pytest.fixture(autouse=True)
def extras(monkeypatch):
    """The optional extras the plugin checks for (tinytuya for the blaster, adb-shell for the Fire TV), as if
    installed: CI runs without them, and nothing here talks to a real device."""
    for name in ("tinytuya", "adb_shell"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
