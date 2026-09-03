"""Behavioural tests for the frankfurter (ECB FX) plugin — base/symbol/config
coercion, the rates fetch/validation, and the convert tool's same-currency
short-circuit + no-rate / bad-amount paths. HTTP is faked; no network.
"""

import asyncio

import pytest

import frankfurter
from frankfurter import FrankfurterPlugin


def _client(payload, *, raise_on_get=False):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if raise_on_get:
                raise RuntimeError("boom")
            return _Resp()

    return _Client


# ── apply_config ─────────────────────────────────────────────────────────────
def test_apply_config_base_and_symbols():
    p = FrankfurterPlugin()
    p.apply_config({"FX_BASE": " usd ", "FX_SYMBOLS": " eur , ,try "})
    assert p.base == "USD"
    assert p.symbols == ["EUR", "TRY"]


def test_apply_config_base_defaults_to_eur():
    p = FrankfurterPlugin()
    p.apply_config({"FX_BASE": ""})
    assert p.base == "EUR"


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("1", True), ("yes", True), ("0", False), ("x", False)],
)
def test_apply_config_sink_bool(value, expected):
    p = FrankfurterPlugin()
    p.apply_config({"FX_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


# ── _latest ──────────────────────────────────────────────────────────────────
def test_latest_returns_rates_dict(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(
        frankfurter.httpx, "AsyncClient", _client({"rates": {"TRY": 34.2}})
    )
    assert asyncio.run(p._latest("USD", ["TRY"])) == {"TRY": 34.2}


def test_latest_none_when_rates_not_a_dict(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(frankfurter.httpx, "AsyncClient", _client({"rates": None}))
    assert asyncio.run(p._latest("USD", ["TRY"])) is None


def test_latest_none_on_error(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(
        frankfurter.httpx, "AsyncClient", _client({}, raise_on_get=True)
    )
    assert asyncio.run(p._latest("USD", ["TRY"])) is None


# ── convert ──────────────────────────────────────────────────────────────────
def test_convert_same_currency_short_circuits_without_fetch(monkeypatch):
    p = FrankfurterPlugin()

    # if _latest were called this would blow up — proves the short-circuit
    def _boom(*a, **k):
        raise AssertionError("must not fetch for same-currency convert")

    monkeypatch.setattr(frankfurter.httpx, "AsyncClient", _boom)
    assert asyncio.run(p.convert(100, "usd", "USD")) == {
        "amount": 100,
        "from": "USD",
        "to": "USD",
        "result": 100,
    }


def test_convert_applies_rate(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(
        frankfurter.httpx, "AsyncClient", _client({"rates": {"TRY": 34.0}})
    )
    assert asyncio.run(p.convert(2, "usd", "try")) == {
        "amount": 2,
        "from": "USD",
        "to": "TRY",
        "result": 68.0,
    }


def test_convert_no_rate_available(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(frankfurter.httpx, "AsyncClient", _client({"rates": {}}))
    assert asyncio.run(p.convert(2, "usd", "xyz")) == {"error": "no rate for USD->XYZ"}


def test_convert_bad_amount(monkeypatch):
    p = FrankfurterPlugin()
    monkeypatch.setattr(
        frankfurter.httpx, "AsyncClient", _client({"rates": {"TRY": 34.0}})
    )
    assert asyncio.run(p.convert("notnum", "usd", "try")) == {
        "error": "amount must be a number"
    }
