"""Behavioural tests for the crypto plugin — symbol/config coercion, the Yahoo
chart parse (incl. the unsafe-symbol guard and non-numeric close skipping), and
the get_price symbol-resolution + fail-soft paths. HTTP is faked; no network.
"""

import asyncio
from datetime import date, datetime, timezone

import pytest

import crypto
from crypto import CryptoPlugin


def _chart_client(payload, *, raise_on_get=False):
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


def _yahoo(timestamps, closes):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


# ── apply_config ─────────────────────────────────────────────────────────────
def test_apply_config_symbols_upper_strip_drop_empty():
    p = CryptoPlugin()
    p.apply_config({"CRYPTO_SYMBOLS": " btc-usd , ,eth-usd "})
    assert p.symbols == ["BTC-USD", "ETH-USD"]


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("1", True), ("yes", True), ("0", False), ("x", False)],
)
def test_apply_config_sink_bool(value, expected):
    p = CryptoPlugin()
    p.apply_config({"CRYPTO_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


# ── _fetch_history parse ─────────────────────────────────────────────────────
def test_fetch_history_rejects_unsafe_symbol(monkeypatch):
    p = CryptoPlugin()
    # even with a client that would return data, the charset guard returns [] first
    monkeypatch.setattr(crypto.httpx, "AsyncClient", _chart_client(_yahoo([1], [1.0])))
    got = asyncio.run(
        p._fetch_history("BTC-USD?inject=1", date(2021, 1, 1), date(2021, 1, 2))
    )
    assert got == []


def test_fetch_history_parses_closes_and_skips_non_numeric(monkeypatch):
    p = CryptoPlugin()
    t0 = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp())
    t1 = t0 + 86400
    t2 = t1 + 86400
    payload = _yahoo([t0, t1, t2], [100.5, None, 200.0])  # None close dropped
    monkeypatch.setattr(crypto.httpx, "AsyncClient", _chart_client(payload))
    out = asyncio.run(p._fetch_history("BTC-USD", date(2021, 1, 1), date(2021, 1, 3)))
    assert [c for _, c in out] == [100.5, 200.0]
    # each ts normalised to that day's 00:00 UTC
    assert out[0][0] == t0 and out[1][0] == t2
    assert all(isinstance(ts, int) for ts, _ in out)


def test_fetch_history_empty_result(monkeypatch):
    p = CryptoPlugin()
    monkeypatch.setattr(
        crypto.httpx, "AsyncClient", _chart_client({"chart": {"result": []}})
    )
    assert (
        asyncio.run(p._fetch_history("BTC-USD", date(2021, 1, 1), date(2021, 1, 2)))
        == []
    )


def test_fetch_history_none_on_error(monkeypatch):
    p = CryptoPlugin()
    monkeypatch.setattr(
        crypto.httpx, "AsyncClient", _chart_client({}, raise_on_get=True)
    )
    assert (
        asyncio.run(p._fetch_history("BTC-USD", date(2021, 1, 1), date(2021, 1, 2)))
        == []
    )


# ── get_price symbol resolution ──────────────────────────────────────────────
def test_get_price_requires_coin():
    assert asyncio.run(CryptoPlugin().get_price("")) == {"error": "coin is required"}


@pytest.mark.parametrize(
    "coin,expected_symbol",
    [
        ("btc", "BTC-USD"),  # alias
        ("bitcoin", "BTC-USD"),  # alias
        ("sol", "SOL-USD"),  # not an alias, no dash → -USD appended
        ("eth-eur", "ETH-EUR"),  # already has a dash → preserved (upper)
    ],
)
def test_get_price_resolves_symbol(monkeypatch, coin, expected_symbol):
    p = CryptoPlugin()
    captured = {}

    async def _fake_fetch(symbol, start, end):
        captured["symbol"] = symbol
        return [(1, 123.0)]

    monkeypatch.setattr(p, "_fetch_history", _fake_fetch)
    res = asyncio.run(p.get_price(coin))
    assert captured["symbol"] == expected_symbol
    assert res == {"symbol": expected_symbol, "close": 123.0}


def test_get_price_unavailable_when_empty(monkeypatch):
    p = CryptoPlugin()

    async def _empty(symbol, start, end):
        return []

    monkeypatch.setattr(p, "_fetch_history", _empty)
    assert asyncio.run(p.get_price("btc")) == {
        "symbol": "BTC-USD",
        "error": "price unavailable",
    }
