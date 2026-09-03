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


class _CaptureClient:
    """Fake httpx.AsyncClient that records the line-protocol body instead of
    hitting the network — used to assert the hand-rolled InfluxDB writers escape
    tag values correctly."""

    last_body = ""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        _CaptureClient.last_body = kw.get("content") or kw.get("data") or ""

        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()


@pytest.mark.parametrize(
    "pkg,method,payload,measurement",
    [
        # a feed/location name carrying line-protocol specials (',' '=' ' ')
        ("news", "_write_influxdb", {"a,b=c d": 3}, "news"),
        (
            "weather",
            "_write_influxdb",
            {"a,b=c d": {"temperature": 1.0, "humidity": 2, "wind_speed": 3}},
            "weather",
        ),
    ],
)
def test_influx_writer_escapes_tag_specials(
    pkg, method, payload, measurement, monkeypatch
):
    """news/weather build InfluxDB line protocol by hand; a config feed/location
    name with ',' '=' or ' ' must be escaped or it silently corrupts the tag set
    (regression: previously only spaces were escaped)."""
    module = importlib.import_module(pkg)
    plugin = _plugin_class(module)()
    plugin.sink_influxdb = True
    plugin.config = {"influxdb": {"enabled": True}}
    monkeypatch.setattr(module.httpx, "AsyncClient", _CaptureClient)

    ok = asyncio.run(getattr(plugin, method)(payload))
    assert ok is True, f"{pkg}: write returned False (body not sent)"
    body = _CaptureClient.last_body
    tag = "a\\,b\\=c\\ d"  # all three specials escaped
    assert (
        f"{measurement}," in body and f"={tag} " in body
    ), f"{pkg}: tag not fully escaped in line protocol: {body!r}"
    assert "=c d" not in body, f"{pkg}: raw unescaped specials leaked: {body!r}"
