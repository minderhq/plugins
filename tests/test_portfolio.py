"""Behavioural tests for the portfolio (per-user holdings) plugin — config
coercion + owner injection, and the owner-scoped get_value read: required/unsafe
owner guards, the influx-disabled short-circuit, the newest-first symbol dedup,
and the fail-soft read-error path. HTTP is faked; no network.
"""

import asyncio

import pytest

import portfolio
from portfolio import PortfolioPlugin


def _post_client(rows, *, raise_on_post=False):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return rows

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if raise_on_post:
                raise RuntimeError("boom")
            return _Resp()

    return _Client


def _enabled(p):
    p.config = {"influxdb": {"enabled": True, "token": "t"}}
    return p


# ── apply_config ─────────────────────────────────────────────────────────────
def test_apply_config_holdings_and_owner_injection():
    p = PortfolioPlugin()
    p.apply_config({"PORTFOLIO_HOLDINGS": " aapl , ,msft ", "_owner_id": "u1"})
    assert p.symbols == ["AAPL", "MSFT"]
    assert p._owner_id == "u1"


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("1", True), ("on", True), ("0", False), ("no", False)],
)
def test_apply_config_sink_bool(value, expected):
    p = PortfolioPlugin()
    p.apply_config({"PORTFOLIO_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


# ── get_value guards ─────────────────────────────────────────────────────────
def test_get_value_requires_owner():
    with pytest.raises(ValueError, match="owner_id is required"):
        asyncio.run(PortfolioPlugin().get_value(""))


def test_get_value_rejects_unsafe_owner():
    # an apostrophe survives the space/comma/equals/newline scrub → fails the
    # charset guard → ValueError, not an InfluxDB SQL injection
    with pytest.raises(ValueError, match="unsafe owner_id"):
        asyncio.run(PortfolioPlugin().get_value("a'b"))


def test_get_value_influx_disabled_short_circuits():
    p = PortfolioPlugin()
    p.config = {"influxdb": {"enabled": False}}
    res = asyncio.run(p.get_value("u1"))
    assert res["message"] == "influxdb not configured"
    assert res["holdings"] == {}


# ── get_value dedup ──────────────────────────────────────────────────────────
def test_get_value_dedupes_newest_first(monkeypatch):
    p = _enabled(PortfolioPlugin())
    rows = [
        {"symbol": "AAPL", "price": 190.0, "time": "2021-01-03"},  # newest AAPL
        {"symbol": "AAPL", "price": 180.0, "time": "2021-01-02"},  # older → dropped
        "not-a-dict",  # skipped
        {"symbol": None, "time": "x"},  # no symbol → skipped
        {"symbol": "MSFT", "price": 300.0, "time": "2021-01-03"},
    ]
    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _post_client(rows))
    res = asyncio.run(p.get_value("u1"))
    assert res["symbol_count"] == 2
    assert res["holdings"]["AAPL"] == {"price": 190.0, "as_of": "2021-01-03"}
    assert res["holdings"]["MSFT"] == {"price": 300.0, "as_of": "2021-01-03"}


def test_get_value_read_error_is_fail_soft(monkeypatch):
    p = _enabled(PortfolioPlugin())
    monkeypatch.setattr(
        portfolio.httpx, "AsyncClient", _post_client([], raise_on_post=True)
    )
    res = asyncio.run(p.get_value("u1"))
    assert res["error"] == "read failed"
    assert res["holdings"] == {}
