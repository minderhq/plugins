"""Behavioural tests for the portfolio (per-user holdings) plugin — config
coercion + owner injection, and the owner-scoped get_value read: required/unsafe
owner guards, the influx-disabled short-circuit, the newest-first symbol dedup,
and the fail-soft read-error path. HTTP is faked; no network.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import portfolio
from portfolio import _SAFE_SYMBOL, PortfolioPlugin


def _post_client(rows, *, raise_on_post=False, capture=None):
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
            if capture is not None:
                capture["json"] = json
                capture["query"] = (json or {}).get("q", "")
            if raise_on_post:
                raise RuntimeError("boom")
            return _Resp()

    return _Client


def _enabled(p):
    p.config = {"influxdb": {"enabled": True, "token": "t"}}
    return p


def _ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


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


def _yahoo_get_client(payload, *, raise_on_get=False):
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


def _write_capture_client(*, raise_on_post=False, capture=None):
    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, headers=None, content=None):
            if capture is not None:
                capture["content"] = content
            if raise_on_post:
                raise RuntimeError("boom")
            return _Resp()

    return _Client


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


def test_unsafe_owner_chars_are_scrubbed_not_rejected(monkeypatch):
    # an owner with SQL/line-protocol metacharacters (apostrophe, and real JWT-sub
    # chars like | @ :) is SCRUBBED to the safe charset — not rejected — so it
    # reads back what the write path (same scrub) stored. No SQL injection.
    p = _enabled(PortfolioPlugin())
    cap = {}
    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _post_client([], capture=cap))
    asyncio.run(p.get_value("auth0|a'b @c"))  # must NOT raise
    # the value is scrubbed to the safe charset (no injection chars leak in); the
    # only quotes in the query are the SQL string delimiters around that value.
    assert "owner_id = 'auth0_a_b__c'" in cap["query"]
    assert "|" not in cap["query"] and "a'b" not in cap["query"]


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


# ── __init__ defaults / owner injection semantics ────────────────────────────
def test_init_defaults():
    p = PortfolioPlugin()
    assert p.symbols == []
    assert p.sink_influxdb is True
    assert p._owner_id is None


def test_apply_config_owner_id_not_a_schema_field_but_settable():
    # _owner_id is injected only by the per-owner scheduler, never part of
    # CONFIG_SCHEMA (not settable via the config API) — but apply_config must
    # still accept it, since that injection is the whole mechanism.
    p = PortfolioPlugin()
    assert "_owner_id" not in [f["key"] for f in p.CONFIG_SCHEMA]
    p.apply_config({"_owner_id": "alice"})
    assert p._owner_id == "alice"


def test_apply_config_owner_id_can_be_reset_to_none():
    p = PortfolioPlugin()
    p.apply_config({"_owner_id": "alice"})
    p.apply_config({"_owner_id": None})
    assert p._owner_id is None


# ── private-per-user contract surface (#920 / #1035) ─────────────────────────
def test_private_per_user_flag_is_set():
    assert PortfolioPlugin.PRIVATE_PER_USER is True


def test_exposes_owner_scoped_read_back_action_only():
    # get_value is JWT-gated (ACTIONS) not unauthenticated (READ_ONLY_ACTIONS),
    # because portfolio data is private; the model supplies no args (owner_id is
    # injected server-side from the JWT).
    assert PortfolioPlugin.ACTIONS == frozenset({"get_value"})
    assert not getattr(PortfolioPlugin, "READ_ONLY_ACTIONS", frozenset())
    tools = {t["name"]: t for t in PortfolioPlugin.AI_TOOLS}
    assert tools["get_portfolio_value"]["action"] == "get_value"
    assert tools["get_portfolio_value"]["parameters"]["properties"] == {}


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_register_and_health():
    p = PortfolioPlugin()
    md = asyncio.run(p.register())
    assert md.name == "portfolio"
    h = asyncio.run(p.health_check())
    assert h["healthy"] is True
    assert h["sink_influxdb"] is p.sink_influxdb


def test_lifecycle_status_transitions():
    p = PortfolioPlugin()
    assert p.status == "registered"
    asyncio.run(p.initialize())
    assert p.status == "ready"
    asyncio.run(p.shutdown())
    assert p.status == "shutdown"


# ── _fetch_latest_close (Yahoo, keyless — same endpoint as crypto) ───────────
def test_fetch_latest_close_returns_most_recent_point(monkeypatch):
    ts = [_ts(2024, 1, 10), _ts(2024, 1, 11), _ts(2024, 1, 12)]
    payload = _yahoo(ts, [100.0, 101.0, 102.5])
    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _yahoo_get_client(payload))
    p = PortfolioPlugin()
    assert asyncio.run(p._fetch_latest_close("AAPL")) == (_ts(2024, 1, 12), 102.5)


def test_fetch_latest_close_skips_trailing_non_numeric_close(monkeypatch):
    # the newest close is non-numeric (a gap/None) → fall back to the prior day.
    ts = [_ts(2024, 1, 10), _ts(2024, 1, 11)]
    payload = _yahoo(ts, [100.0, None])
    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _yahoo_get_client(payload))
    p = PortfolioPlugin()
    assert asyncio.run(p._fetch_latest_close("AAPL")) == (_ts(2024, 1, 10), 100.0)


@pytest.mark.parametrize(
    "payload",
    [{}, {"chart": {}}, {"chart": {"result": []}}, {"chart": {"result": None}}],
)
def test_fetch_latest_close_empty_or_missing_result(monkeypatch, payload):
    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _yahoo_get_client(payload))
    p = PortfolioPlugin()
    assert asyncio.run(p._fetch_latest_close("AAPL")) is None


def test_fetch_latest_close_none_on_http_exception(monkeypatch):
    monkeypatch.setattr(
        portfolio.httpx, "AsyncClient", _yahoo_get_client({}, raise_on_get=True)
    )
    p = PortfolioPlugin()
    assert asyncio.run(p._fetch_latest_close("AAPL")) is None


def test_fetch_latest_close_unsafe_symbol_skips_http_call(monkeypatch, caplog):
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **kw):
            called["n"] += 1

    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _Boom)
    p = PortfolioPlugin()
    assert not _SAFE_SYMBOL.match("AAPL?evil=1")
    with caplog.at_level("WARNING"):
        out = asyncio.run(p._fetch_latest_close("AAPL?evil=1"))
    assert out is None
    assert called["n"] == 0
    assert any("unsafe symbol" in r.message for r in caplog.records)


# ── _influx_cfg gating ───────────────────────────────────────────────────────
def test_influx_cfg_none_when_sink_disabled():
    p = PortfolioPlugin()
    p.sink_influxdb = False
    p.config = {"influxdb": {"enabled": True}}
    assert p._influx_cfg() is None


def test_influx_cfg_returns_cfg_when_enabled():
    p = PortfolioPlugin()
    p.sink_influxdb = True
    cfg = {"enabled": True, "host": "h", "port": 1}
    p.config = {"influxdb": cfg}
    assert p._influx_cfg() is cfg


# ── _write_holding — the per-user tenancy guarantee this plugin exists for ───
def test_write_holding_refuses_without_owner_id_set(monkeypatch, caplog):
    # The core guarantee: never write under a blank/shared owner just because a
    # direct collect_data() ran outside the per-owner scheduler path.
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **kw):
            called["n"] += 1

    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _Boom)
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    assert p._owner_id is None
    with caplog.at_level("WARNING"):
        out = asyncio.run(p._write_holding("AAPL", 1700000000, 150.0))
    assert out is False
    assert called["n"] == 0
    assert any("owner_id" in r.message for r in caplog.records)


def test_write_holding_false_when_cfg_none():
    p = PortfolioPlugin()
    p.sink_influxdb = False
    p._owner_id = "alice"
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is False


def test_write_holding_unsafe_symbol_skips_http_call(monkeypatch, caplog):
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **kw):
            called["n"] += 1

    monkeypatch.setattr(portfolio.httpx, "AsyncClient", _Boom)
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    p._owner_id = "alice"
    assert not _SAFE_SYMBOL.match("AAPL'; DROP TABLE x --")
    with caplog.at_level("WARNING"):
        out = asyncio.run(p._write_holding("AAPL'; DROP TABLE x --", 1, 1.0))
    assert out is False
    assert called["n"] == 0


def test_write_holding_success_tags_owner_id_in_line_protocol(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        portfolio.httpx, "AsyncClient", _write_capture_client(capture=cap)
    )
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True, "host": "h", "port": 1}}
    p._owner_id = "alice"
    out = asyncio.run(p._write_holding("AAPL", 1700000000, 150.0))
    assert out is True
    assert "owner_id=alice" in cap["content"]
    assert "symbol=AAPL" in cap["content"]


def test_write_holding_sanitizes_owner_id_metacharacters(monkeypatch):
    # a JWT sub with line-protocol metacharacters (space, comma, equals) must be
    # scrubbed to the safe charset before it lands as a tag — same scrub as read.
    cap = {}
    monkeypatch.setattr(
        portfolio.httpx, "AsyncClient", _write_capture_client(capture=cap)
    )
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True, "host": "h", "port": 1}}
    p._owner_id = "ali ce,x=y"
    asyncio.run(p._write_holding("AAPL", 1700000000, 150.0))
    owner_field = cap["content"].split("owner_id=")[1].split(" ")[0]
    assert "," not in owner_field and "=" not in owner_field and " " not in owner_field


def test_write_holding_false_on_http_exception(monkeypatch):
    monkeypatch.setattr(
        portfolio.httpx, "AsyncClient", _write_capture_client(raise_on_post=True)
    )
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    p._owner_id = "alice"
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is False


# ── collect_data (per-owner) / analyze ───────────────────────────────────────
def test_collect_data_aggregates_across_symbols_for_current_owner(monkeypatch):
    p = PortfolioPlugin()
    p.symbols = ["AAPL", "BTC-USD"]
    p._owner_id = "alice"
    fetch_map = {"AAPL": (1700000000, 150.0), "BTC-USD": None}

    async def fake_fetch(symbol):
        return fetch_map[symbol]

    async def fake_write(symbol, ts, close):
        return True

    monkeypatch.setattr(p, "_fetch_latest_close", fake_fetch)
    monkeypatch.setattr(p, "_write_holding", fake_write)
    result = asyncio.run(p.collect_data())
    assert result["owner_id"] == "alice"
    assert result["symbols"]["AAPL"] == {"written": True, "latest_close": 150.0}
    assert result["symbols"]["BTC-USD"] == {
        "written": False,
        "error": "price unavailable",
    }
    assert p._last == result


def test_collect_data_sets_collected_at_iso_timestamp(monkeypatch):
    p = PortfolioPlugin()
    p.symbols = ["AAPL"]
    p._owner_id = "alice"
    monkeypatch.setattr(p, "_fetch_latest_close", AsyncMock(return_value=None))
    result = asyncio.run(p.collect_data())
    datetime.fromisoformat(result["collected_at"])  # must be real ISO


def test_collect_data_empty_symbols_returns_empty_result():
    p = PortfolioPlugin()
    p.symbols = []
    p._owner_id = "alice"
    result = asyncio.run(p.collect_data())
    assert result["symbols"] == {}
    assert result["owner_id"] == "alice"


def test_analyze_before_any_collection():
    p = PortfolioPlugin()
    assert asyncio.run(p.analyze()) == {"message": "no data collected yet"}


def test_analyze_returns_last_collection(monkeypatch):
    p = PortfolioPlugin()
    p.symbols = ["AAPL"]
    p._owner_id = "alice"
    monkeypatch.setattr(p, "_fetch_latest_close", AsyncMock(return_value=None))
    asyncio.run(p.collect_data())
    assert asyncio.run(p.analyze()) == p._last
