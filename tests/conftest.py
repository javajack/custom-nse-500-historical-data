"""Shared test infrastructure.

The big concern: NSE_UNIVERSE_DATA_DIR is read at module-import time in
`nse_universe.paths`, so tests that need an isolated data dir have to reload
the modules. That reload is sticky — without explicit cleanup, the rest of
the pytest session sees the temp path. This conftest exposes a fixture that
sets up *and* tears down isolation cleanly.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


_MODULES_TO_RELOAD = (
    "nse_universe.paths",
    "nse_universe.core.db",
    "nse_universe.core.export",
    "nse_universe.rank.filters",
    "nse_universe.rank.v2",
    "nse_universe.core.universe",
)


def _reload_paths_chain() -> None:
    """Reload paths + all consumers so they pick up the current env."""
    import nse_universe.paths  # noqa: F401
    for modname in _MODULES_TO_RELOAD:
        try:
            mod = __import__(modname, fromlist=["_"])
            importlib.reload(mod)
        except Exception:
            # The first reload of `paths` may invalidate the dotted lookup
            # for already-bound symbols; ignore — the next reload picks up.
            pass


@pytest.fixture
def isolated_data_dir(tmp_path: Path):
    """Point nse_universe at an isolated data dir for one test.

    Sets NSE_UNIVERSE_DATA_DIR, reloads dependent modules so the change
    takes effect, then on teardown restores the prior env and reloads
    again to undo the path binding.
    """
    old = os.environ.get("NSE_UNIVERSE_DATA_DIR")
    os.environ["NSE_UNIVERSE_DATA_DIR"] = str(tmp_path)
    _reload_paths_chain()
    try:
        yield tmp_path
    finally:
        if old is None:
            os.environ.pop("NSE_UNIVERSE_DATA_DIR", None)
        else:
            os.environ["NSE_UNIVERSE_DATA_DIR"] = old
        _reload_paths_chain()
