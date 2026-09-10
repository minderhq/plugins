"""Behavioural tests for the crypto plugin — symbol/config coercion, the Yahoo
chart parse (incl. the unsafe-symbol guard and non-numeric close skipping), and
the get_price symbol-resolution + fail-soft paths. HTTP is faked; no network.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import crypto
from crypto import _ALIASES, _EARLIEST, CryptoPlugin


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


def test_get_price_uses_last_point_close_and_seven_day_window(monkeypatch):
    p = CryptoPlugin()
    window = {}

    async def _fake(symbol, start, end):
        window["start"] = start
        window["end"] = end
        return [(1, 10.0), (2, 20.0), (3, 30.0)]

    monkeypatch.setattr(p, "_fetch_history", _fake)
    out = asyncio.run(p.get_price("BTC-USD"))
    assert out == {"symbol": "BTC-USD", "close": 30.0}
    assert (window["end"] - window["start"]).days == 7


# ── __init__ defaults / apply_config extras ──────────────────────────────────
def test_init_defaults():
    p = CryptoPlugin()
    assert p.symbols == ["BTC-USD", "ETH-USD"]
    assert p.start_date == ""
    assert p.sink_influxdb is True


def test_apply_config_start_date_stripped():
    p = CryptoPlugin()
    p.apply_config({"CRYPTO_START_DATE": " 2020-05-01 "})
    assert p.start_date == "2020-05-01"


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_register_and_health():
    p = CryptoPlugin()
    md = asyncio.run(p.register())
    assert md.name == "crypto"
    h = asyncio.run(p.health_check())
    assert h["healthy"] is True
    assert h["symbols"] == p.symbols
    assert h["influxdb_sink"] is p.sink_influxdb


def test_lifecycle_status_transitions():
    p = CryptoPlugin()
    assert p.status == "registered"
    asyncio.run(p.initialize())
    assert p.status == "ready"
    asyncio.run(p.shutdown())
    assert p.status == "shutdown"


# ── _fetch_history — extra empty/missing shapes ──────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"chart": {}},
        {"chart": {"result": None}},
        {"chart": {"result": [None]}},
    ],
)
def test_fetch_history_empty_or_missing_result_shapes(monkeypatch, payload):
    p = CryptoPlugin()
    monkeypatch.setattr(crypto.httpx, "AsyncClient", _chart_client(payload))
    out = asyncio.run(p._fetch_history("BTC-USD", date(2021, 1, 1), date(2021, 1, 2)))
    assert out == []


# ── _influx_cfg gating (pure plugin logic) ───────────────────────────────────
def test_influx_cfg_none_when_sink_disabled():
    p = CryptoPlugin()
    p.sink_influxdb = False
    p.config = {"influxdb": {"enabled": True}}
    assert p._influx_cfg() is None


def test_influx_cfg_none_when_influxdb_not_enabled():
    p = CryptoPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": False}}
    assert p._influx_cfg() is None


def test_influx_cfg_none_when_influxdb_missing():
    p = CryptoPlugin()
    p.sink_influxdb = True
    p.config = {}
    assert p._influx_cfg() is None


def test_influx_cfg_returns_cfg_when_enabled():
    p = CryptoPlugin()
    p.sink_influxdb = True
    cfg = {"enabled": True, "host": "h", "port": 1}
    p.config = {"influxdb": cfg}
    assert p._influx_cfg() is cfg


# The InfluxDB read/write helpers are the SDK's (tested in the SDK's own
# test_influx.py); here we only assert the plugin's own gating short-circuits
# before any network path when the sink is off / there's nothing to write.
def test_latest_influx_date_none_when_sink_off():
    p = CryptoPlugin()
    p.sink_influxdb = False
    assert asyncio.run(p._latest_influx_date("BTC-USD")) is None


def test_write_history_zero_when_sink_off():
    p = CryptoPlugin()
    p.sink_influxdb = False
    assert asyncio.run(p._write_history("BTC-USD", [(1, 2.0)])) == 0


def test_write_history_zero_when_points_empty():
    p = CryptoPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    assert asyncio.run(p._write_history("BTC-USD", [])) == 0


# ── collect_data — the state-dependent backfill-vs-append branching ──────────
def _today():
    return datetime.now(timezone.utc).date()


def test_collect_data_incremental_appends_from_latest_plus_one(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    yesterday = _today() - timedelta(days=1)
    fetch_calls = []

    async def fake_latest(symbol):
        return yesterday

    async def fake_fetch(symbol, start, end):
        fetch_calls.append((symbol, start, end))
        return [(1, 42.0)]

    async def fake_write(symbol, points):
        return len(points)

    monkeypatch.setattr(p, "_latest_influx_date", fake_latest)
    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", fake_write)

    result = asyncio.run(p.collect_data())

    assert fetch_calls == [("BTC-USD", _today(), _today())]
    assert result["symbols"]["BTC-USD"]["from"] == _today().isoformat()
    assert result["symbols"]["BTC-USD"]["written"] == 1
    assert result["symbols"]["BTC-USD"]["latest_close"] == 42.0
    assert "up_to_date" not in result["symbols"]["BTC-USD"]


def test_collect_data_up_to_date_when_start_is_in_future(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    fetch_called = {"n": 0}

    async def fake_latest(symbol):
        return _today()  # start = today + 1 > today

    async def fake_fetch(symbol, start, end):
        fetch_called["n"] += 1
        return [(1, 42.0)]

    monkeypatch.setattr(p, "_latest_influx_date", fake_latest)
    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))

    result = asyncio.run(p.collect_data())

    assert fetch_called["n"] == 0
    assert result["symbols"]["BTC-USD"] == {"written": 0, "up_to_date": True}


def test_collect_data_uses_configured_start_date_when_no_history(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    p.start_date = "2019-06-15"
    fetch_calls = []

    async def fake_fetch(symbol, start, end):
        fetch_calls.append(start)
        return []

    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))

    result = asyncio.run(p.collect_data())

    assert fetch_calls == [date(2019, 6, 15)]
    assert result["symbols"]["BTC-USD"]["from"] == "2019-06-15"


@pytest.mark.parametrize("bad_start", ["not-a-date", ""])
def test_collect_data_falls_back_to_earliest(monkeypatch, bad_start):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    p.start_date = bad_start
    fetch_calls = []

    async def fake_fetch(symbol, start, end):
        fetch_calls.append(start)
        return []

    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))

    result = asyncio.run(p.collect_data())

    assert fetch_calls == [_EARLIEST]
    assert result["symbols"]["BTC-USD"]["from"] == _EARLIEST.isoformat()


def test_collect_data_aggregates_symbols_independently(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD", "ETH-USD"]
    p.start_date = ""
    latest_map = {"BTC-USD": None, "ETH-USD": date(2024, 1, 1)}
    fetch_map = {"BTC-USD": [(1, 10.0), (2, 11.0)], "ETH-USD": []}
    write_map = {"BTC-USD": 2, "ETH-USD": 0}

    async def fake_latest(symbol):
        return latest_map[symbol]

    async def fake_fetch(symbol, start, end):
        return fetch_map[symbol]

    async def fake_write(symbol, points):
        return write_map[symbol]

    monkeypatch.setattr(p, "_latest_influx_date", fake_latest)
    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", fake_write)

    result = asyncio.run(p.collect_data())

    assert result["symbols"]["BTC-USD"]["from"] == _EARLIEST.isoformat()
    assert result["symbols"]["BTC-USD"]["written"] == 2
    assert result["symbols"]["BTC-USD"]["latest_close"] == 11.0
    assert result["symbols"]["ETH-USD"]["from"] == date(2024, 1, 2).isoformat()
    assert result["symbols"]["ETH-USD"]["written"] == 0
    assert result["symbols"]["ETH-USD"]["latest_close"] is None
    assert set(result["symbols"]) == {"BTC-USD", "ETH-USD"}
    assert p._last == result


def test_collect_data_sets_last_with_symbols_and_collected_at(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    p.start_date = ""
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    monkeypatch.setattr(p, "_fetch_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))

    result = asyncio.run(p.collect_data())

    assert set(result.keys()) == {"symbols", "collected_at"}
    assert "BTC-USD" in result["symbols"]
    datetime.fromisoformat(result["collected_at"])  # parseable ISO timestamp
    assert p._last == result


# ── analyze / refresh ────────────────────────────────────────────────────────
def test_analyze_before_any_collection():
    p = CryptoPlugin()
    out = asyncio.run(p.analyze())
    assert out["message"] == "no data collected yet"
    assert out["symbols"] == p.symbols


def test_analyze_returns_last_collection(monkeypatch):
    p = CryptoPlugin()
    p.symbols = ["BTC-USD"]
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    monkeypatch.setattr(p, "_fetch_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))
    asyncio.run(p.collect_data())
    assert asyncio.run(p.analyze()) == p._last


def test_refresh_calls_collect_data(monkeypatch):
    p = CryptoPlugin()
    monkeypatch.setattr(p, "collect_data", AsyncMock(return_value={"ok": True}))
    assert asyncio.run(p.refresh()) == {"ok": True}


def test_get_price_alias_table_matches_resolution(monkeypatch):
    p = CryptoPlugin()
    for coin, symbol in _ALIASES.items():
        seen = {}

        async def fake_fetch(sym, start, end):
            seen["symbol"] = sym
            return [(1, 1.0)]

        monkeypatch.setattr(p, "_fetch_history", fake_fetch)
        asyncio.run(p.get_price(coin))
        assert seen["symbol"] == symbol
