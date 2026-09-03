"""Validate EVERY plugin in the catalog against the SDK contract.

Discovers each top-level ``<name>/__init__.py`` package, loads its plugin class,
and runs ``check_plugin``. Adding a plugin dir automatically brings it under test —
a broken plugin fails CI.
"""

import asyncio
import importlib
import inspect
import sys
from pathlib import Path

import pytest

from minder_plugin_sdk import Plugin, check_plugin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SKIP = {"tests", "__pycache__"}


def _plugin_packages():
    return sorted(
        p.name
        for p in ROOT.iterdir()
        if p.is_dir() and p.name not in _SKIP and (p / "__init__.py").exists()
    )


def _plugin_class(module):
    names = getattr(module, "__all__", None)
    if names:
        return getattr(module, names[0])
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ == module.__name__ and hasattr(obj, "register"):
            return obj
    raise AssertionError(f"{module.__name__}: no plugin class with register()")


def test_catalog_is_non_empty():
    assert _plugin_packages(), "the catalog has no plugins"


@pytest.mark.parametrize("pkg", _plugin_packages())
def test_plugin_honours_the_contract(pkg):
    module = importlib.import_module(pkg)
    cls = _plugin_class(module)
    plugin = cls()
    assert isinstance(plugin, Plugin) or hasattr(plugin, "register")
    problems = check_plugin(plugin)
    assert problems == [], f"{pkg}: {problems}"


@pytest.mark.parametrize("pkg", _plugin_packages())
def test_plugin_lifecycle_runtime(pkg):
    """Run the lifecycle for real: register() must return usable metadata and
    health_check() must honour the {"healthy": bool} contract (the easy-to-miss
    rule the monitoring loop depends on)."""
    module = importlib.import_module(pkg)
    plugin = _plugin_class(module)()
    meta = asyncio.run(plugin.register())
    assert getattr(meta, "name", None) and getattr(
        meta, "version", None
    ), f"{pkg}: register() returned incomplete metadata: {meta!r}"
    if hasattr(plugin, "health_check"):
        health = asyncio.run(plugin.health_check())
        assert isinstance(health, dict) and isinstance(
            health.get("healthy"), bool
        ), f"{pkg}: health_check() must return {{'healthy': bool}}, got {health!r}"
