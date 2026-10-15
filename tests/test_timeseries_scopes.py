"""Catalog-wide time-series rules.

Every time-series writer declares ``TIMESERIES = {"measurements": [...]}`` and
writes through the handle the platform binds (``bind_timeseries``), never with
an InfluxDB address or token from its config. The platform refuses a plugin
whose scope overlaps another loaded plugin's, so the catalog's declarations
must not overlap: no measurement declared twice, and none inside another
plugin's default scope (an undeclared plugin may use ``<name>`` and
``<name>_*``, its directory name with non-word characters as ``_``).
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_catalog  # noqa: E402

_WRITERS = {
    "crypto": ["crypto_price"],
    "frankfurter": ["fx_rate"],
    "github": ["github_repo"],
    "news": ["news"],
    "portfolio": ["portfolio_holding"],
    "tefas_funds": ["tefas_fund_price"],
    "weather": ["weather"],
}


def _plugins():
    out = {}
    for pkg in gen_catalog._plugin_packages():
        module = importlib.import_module(pkg)
        out[pkg] = gen_catalog._plugin_class(module)
    return out


def _prefix(pkg):
    return re.sub(r"[^A-Za-z0-9_]", "_", pkg)


def test_every_writer_declares_its_measurements_and_takes_a_handle():
    plugins = _plugins()
    for pkg, measurements in _WRITERS.items():
        cls = plugins[pkg]
        assert cls.TIMESERIES == {"measurements": measurements}, pkg
        assert callable(getattr(cls, "bind_timeseries", None)), pkg


def test_declared_measurements_do_not_overlap():
    plugins = _plugins()
    declared = {
        pkg: (getattr(cls, "TIMESERIES", None) or {}).get("measurements")
        for pkg, cls in plugins.items()
    }
    seen = {}
    for pkg, names in declared.items():
        for name in names or ():
            assert name not in seen, f"{name!r} declared by {seen.get(name)} and {pkg}"
            seen[name] = pkg
    for name, owner in seen.items():
        for pkg, names in declared.items():
            if pkg == owner or names is not None:
                continue
            prefix = _prefix(pkg)
            assert not (
                name == prefix or name.startswith(prefix + "_")
            ), f"{owner}'s {name!r} falls in {pkg}'s default scope"


@pytest.mark.parametrize("pkg", sorted(_WRITERS))
def test_writers_hold_no_raw_influx_credentials(pkg):
    source = (ROOT / pkg / "__init__.py").read_text(encoding="utf-8")
    assert 'config.get("influxdb")' not in source
    assert "/api/v2/write" not in source and "query_sql" not in source
    assert "latest_influx_date(" not in source.replace("_latest_influx_date(", "")
