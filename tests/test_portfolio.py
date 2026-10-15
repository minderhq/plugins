"""Behavioural tests for the portfolio (per-user holdings) plugin — config
coercion + owner injection, the owner-scoped write through the platform's
time-series handle, and the owner-scoped get_value read of the last point written
per owner. HTTP is faked; no network.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import portfolio
from portfolio import _SAFE_SYMBOL, PortfolioPlugin


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


def _writing(bind_timeseries, owner="alice", **kwargs):
    p = PortfolioPlugin()
    p.sink_influxdb = True
    p._owner_id = owner
    ts = bind_timeseries(p, **kwargs)
    ts.owner = owner  # the owner the platform acts for while collecting
    return p, ts


def _collect(p, ts, owner, closes, monkeypatch):
    """One scheduled per-owner collection: the platform sets the owner on the
    plugin (apply_config) and acts for it on the store."""
    p.apply_config({"PORTFOLIO_HOLDINGS": ",".join(closes), "_owner_id": owner})
    ts.owner = owner

    async def fake_fetch(symbol):
        return closes[symbol]

    monkeypatch.setattr(p, "_fetch_latest_close", fake_fetch)
    return asyncio.run(p.collect_data())


def _read(p, ts, owner):
    ts.owner = owner  # the platform reads for the calling owner only
    return asyncio.run(p.get_value(owner))


def test_get_value_without_a_handle_says_storage_is_unavailable():
    res = asyncio.run(PortfolioPlugin().get_value("u1"))
    assert res["holdings"] == {} and res["symbol_count"] == 0
    assert "not available" in res["message"]


def test_get_value_before_any_collection_is_empty(bind_timeseries):
    p, ts = _writing(bind_timeseries)
    res = _read(p, ts, "alice")
    assert res["holdings"] == {} and "no price recorded" in res["message"]


def test_get_value_reads_the_newest_points_from_the_store(bind_timeseries, monkeypatch):
    p, ts = _writing(bind_timeseries)
    _collect(p, ts, "alice", {"AAPL": (_ts(2021, 1, 1), 180.0)}, monkeypatch)
    _collect(
        p,
        ts,
        "alice",
        {"AAPL": (_ts(2021, 1, 2), 190.0), "MSFT": (_ts(2021, 1, 2), 300.0)},
        monkeypatch,
    )
    res = _read(p, ts, "alice")
    assert res["symbol_count"] == 2 and "message" not in res
    assert res["holdings"]["AAPL"] == {"price": 190.0, "as_of": "2021-01-02T00:00:00"}
    assert res["holdings"]["MSFT"]["price"] == 300.0
    # Value reads through latest(..., fields=[...]): the index, then each symbol.
    assert ts.latest_fields_calls == [
        ("portfolio_index", "kind", "holdings", ["symbols"]),
        ("portfolio_holding", "symbol", "AAPL", ["price"]),
        ("portfolio_holding", "symbol", "MSFT", ["price"]),
    ]


def test_get_value_survives_a_restart(bind_timeseries, monkeypatch):
    """A new plugin instance (a restart) bound to the same store answers from
    the store, not from anything held in memory."""
    p, ts = _writing(bind_timeseries)
    _collect(p, ts, "alice", {"AAPL": (_ts(2021, 1, 2), 190.0)}, monkeypatch)
    restarted = PortfolioPlugin()
    restarted.bind_timeseries(ts)
    res = _read(restarted, ts, "alice")
    assert res["holdings"] == {"AAPL": {"price": 190.0, "as_of": "2021-01-02T00:00:00"}}


def test_get_value_never_returns_another_owners_holdings(bind_timeseries, monkeypatch):
    p, ts = _writing(bind_timeseries)
    _collect(p, ts, "alice", {"AAPL": (_ts(2021, 1, 1), 180.0)}, monkeypatch)
    _collect(p, ts, "bob", {"TSLA": (_ts(2021, 1, 1), 700.0)}, monkeypatch)
    assert set(_read(p, ts, "alice")["holdings"]) == {"AAPL"}
    assert set(_read(p, ts, "bob")["holdings"]) == {"TSLA"}
    assert _read(p, ts, "carol")["holdings"] == {}


def test_get_value_drops_symbols_the_owner_no_longer_tracks(
    bind_timeseries, monkeypatch
):
    p, ts = _writing(bind_timeseries)
    _collect(
        p,
        ts,
        "alice",
        {"AAPL": (_ts(2021, 1, 1), 180.0), "MSFT": (_ts(2021, 1, 1), 300.0)},
        monkeypatch,
    )
    _collect(p, ts, "alice", {"AAPL": (_ts(2021, 1, 2), 181.0)}, monkeypatch)
    assert set(_read(p, ts, "alice")["holdings"]) == {"AAPL"}


def test_get_value_skips_unsafe_or_unpriced_index_entries(bind_timeseries):
    p, ts = _writing(bind_timeseries)
    asyncio.run(p._write_holding("AAPL", _ts(2021, 1, 1), 1.0))
    ts.points[("alice", "portfolio_index", "kind", "holdings")] = (
        _ts(2021, 1, 1),
        {"symbols": "AAPL,bad symbol',NOPRICE"},
    )
    res = _read(p, ts, "alice")
    assert set(res["holdings"]) == {"AAPL"}
    assert [c[2] for c in ts.latest_fields_calls[1:]] == ["AAPL", "NOPRICE"]


def test_get_value_read_failure_is_a_marker_not_a_raise(bind_timeseries):
    p, ts = _writing(bind_timeseries, fail_latest=RuntimeError("store down, x=y"))
    res = _read(p, ts, "alice")
    assert res == {
        "owner_id": "alice",
        "holdings": {},
        "symbol_count": 0,
        "error": "read failed",
    }


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
    # No time-series handle bound: nothing is written, whatever the toggle says.
    assert h["sink_influxdb"] is False and h["timeseries_bound"] is False
    p.bind_timeseries(object())
    h = asyncio.run(p.health_check())
    assert h["sink_influxdb"] is p.sink_influxdb and h["timeseries_bound"] is True


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


# ── _write_holding — the per-user tenancy guarantee this plugin exists for ───
def test_declares_its_measurement_and_holds_no_influx_credentials():
    p = PortfolioPlugin({"influxdb": {"enabled": True, "token": "t"}})
    p._owner_id = "alice"
    assert p.TIMESERIES == {"measurements": ["portfolio_holding", "portfolio_index"]}
    assert not hasattr(p, "_influx_cfg")
    # No handle bound: nothing is written, whatever the config holds.
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is False


def test_write_holding_refuses_without_owner_id_set(bind_timeseries, caplog):
    # The core guarantee: never write under a blank/shared owner just because a
    # direct collect_data() ran outside the per-owner scheduler path.
    p, ts = _writing(bind_timeseries, owner=None)
    with caplog.at_level("WARNING"):
        out = asyncio.run(p._write_holding("AAPL", 1700000000, 150.0))
    assert out is False
    assert ts.batches == []
    assert any("owner_id" in r.message for r in caplog.records)


def test_write_holding_false_when_sink_disabled(bind_timeseries):
    p, ts = _writing(bind_timeseries)
    p.sink_influxdb = False
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is False
    assert ts.batches == []


def test_write_holding_unsafe_symbol_skips_the_write(bind_timeseries):
    p, ts = _writing(bind_timeseries)
    assert not _SAFE_SYMBOL.match("AAPL'; DROP TABLE x --")
    assert asyncio.run(p._write_holding("AAPL'; DROP TABLE x --", 1, 1.0)) is False
    assert ts.batches == []


def test_write_holding_leaves_the_owner_tag_to_the_platform(bind_timeseries):
    # The platform forces owner_id (and plugin) on every point; the plugin sends
    # only its own tags, so it cannot name another owner.
    p, ts = _writing(bind_timeseries)
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is True
    assert ts.batches == [
        (["portfolio_holding,symbol=AAPL price=150.0 1700000000"], "s")
    ]
    assert "owner_id" not in ts.lines[0]


def test_write_holding_false_on_write_failure_and_stores_nothing(bind_timeseries):
    p, ts = _writing(bind_timeseries, fail_write=RuntimeError("boom"))
    assert asyncio.run(p._write_holding("AAPL", 1700000000, 150.0)) is False
    assert ts.points == {}


def test_collect_writes_the_owners_index(bind_timeseries, monkeypatch):
    p, ts = _writing(bind_timeseries)
    result = _collect(
        p, ts, "alice", {"AAPL": (1700000000, 1.0), "MSFT": None}, monkeypatch
    )
    assert result["indexed"] is True
    (index_line,) = [line for line in ts.lines if line.startswith("portfolio_index")]
    assert index_line.startswith('portfolio_index,kind=holdings symbols="AAPL,MSFT" ')
    assert "owner_id" not in index_line


def test_index_is_not_written_without_an_owner_or_with_the_sink_off(bind_timeseries):
    p, ts = _writing(bind_timeseries, owner=None)
    p.symbols = ["AAPL"]
    assert asyncio.run(p._write_index()) is False
    p._owner_id, p.sink_influxdb = "alice", False
    assert asyncio.run(p._write_index()) is False
    assert ts.batches == []


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


# ── bounds and refusals ──────────────────────────────────────────────────────
def test_apply_config_caps_and_dedupes_symbols(caplog):
    p = PortfolioPlugin()
    many = ",".join(f"S{i}" for i in range(portfolio.MAX_SYMBOLS + 7))
    with caplog.at_level("WARNING"):
        p.apply_config({"PORTFOLIO_HOLDINGS": "AAPL,aapl," + many})
    assert len(p.symbols) == portfolio.MAX_SYMBOLS
    assert p.symbols[0] == "AAPL" and p.symbols.count("AAPL") == 1
    assert "at most" in caplog.text


def test_get_value_reads_at_most_max_symbols_concurrently_bounded(
    bind_timeseries, monkeypatch
):
    p, ts = _writing(bind_timeseries)
    symbols = [f"S{i}" for i in range(portfolio.MAX_SYMBOLS + 10)]
    ts.points[("alice", "portfolio_index", "kind", "holdings")] = (
        1,
        {"symbols": ",".join(symbols)},
    )
    for sym in symbols:
        ts.points[("alice", "portfolio_holding", "symbol", sym)] = (1, {"price": 1.0})
    inflight = {"now": 0, "max": 0}
    real_latest = ts.latest

    async def tracking(*args, **kwargs):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0)
        try:
            return await real_latest(*args, **kwargs)
        finally:
            inflight["now"] -= 1

    monkeypatch.setattr(ts, "latest", tracking)
    res = _read(p, ts, "alice")
    assert res["symbol_count"] == portfolio.MAX_SYMBOLS
    assert 1 < inflight["max"] <= portfolio._READ_CONCURRENCY


def test_write_index_caps_the_symbol_list(bind_timeseries):
    p, ts = _writing(bind_timeseries)
    p.symbols = [f"S{i}" for i in range(portfolio.MAX_SYMBOLS + 5)]
    assert asyncio.run(p._write_index()) is True
    (line,) = ts.lines
    assert line.count(",") - 1 == portfolio.MAX_SYMBOLS - 1  # 1 comma for the tag


class TimeseriesDenied(Exception):
    """Stands in for the platform's refusal (matched by type name)."""

    reason = "portfolio may not read measurement 'x'"


class CapabilityInvalid(Exception):
    reason = "fields must be plain field names"


@pytest.mark.parametrize(
    "exc, marker",
    [(TimeseriesDenied(), "read refused"), (CapabilityInvalid(), "read rejected")],
)
def test_get_value_logs_a_refusal_as_an_error(bind_timeseries, caplog, exc, marker):
    p, ts = _writing(bind_timeseries, fail_latest=exc)
    with caplog.at_level("WARNING"):
        res = _read(p, ts, "alice")
    assert res["error"] == marker and res["holdings"] == {}
    (record,) = [r for r in caplog.records if r.levelname == "ERROR"]
    assert type(exc).__name__ in record.getMessage()
    assert exc.reason in record.getMessage()
