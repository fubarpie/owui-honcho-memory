"""Shared pytest helpers: load parts/01_client.py (name starts with a digit, so use importlib)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_client_module():
    """Import parts/01_client.py as module ``honcho_client_part``."""
    name = "honcho_client_part"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "parts" / "01_client.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits a real Honcho server (needs HONCHO_TOKEN)")


@pytest.fixture(scope="session")
def client_mod():
    return load_client_module()


@pytest.fixture(autouse=True)
def _clear_caches(client_mod):
    for c in (client_mod.HonchoClient._known_workspaces, client_mod.HonchoClient._known_peers,
              client_mod.HonchoClient._known_sessions):
        c.clear()
    yield
