"""Behavioural tests for the tefas_funds plugin — fund-code/config coercion, the
blocking tefas-crawler parse (date coercion, None-price skip, fail-soft on fetch
and parse errors), the TEFAS_AVAILABLE gate, and get_fund_price. The Crawler is
faked; no network and no real tefas-crawler needed.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import tefas_funds
from tefas_funds import _DEFAULT_START, TefasPlugin


class _FakeDF:
    """Minimal stand-in for the tefas-crawler pandas DataFrame: iterrows() yields
    (index, row) where row is a dict supporting row["date"] / row["price"]."""

    def __init__(self, rows, *, raise_on_iter=False):
        self._rows = rows
        self._raise = raise_on_iter

    def iterrows(self):
        if self._raise:
            raise RuntimeError("parse boom")
        return enumerate(self._rows)


def _fake_crawler(df=None, *, raise_on_fetch=False):
    class _Crawler:
        def __init__(self, *a, **k):
            pass

        def fetch(self, start, end, name, columns):
            if raise_on_fetch:
                raise RuntimeError("fetch boom")
            return df

    return _Crawler


# ── apply_config ─────────────────────────────────────────────────────────────
def test_apply_config_funds_upper_strip_drop_empty():
    p = TefasPlugin()
    p.apply_config({"TEFAS_FUNDS": " yac , , tte "})
    assert p.funds == ["YAC", "TTE"]


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("1", True), ("on", True), ("0", False), ("no", False)],
)
def test_apply_config_sink_bool(value, expected):
    p = TefasPlugin()
    p.apply_config({"TEFAS_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


def test_apply_config_funds_default_empty():
    p = TefasPlugin()
    p.apply_config({"TEFAS_FUNDS": ""})
    assert p.funds == []


def test_apply_config_start_date_passthrough():
    p = TefasPlugin()
    p.apply_config({"TEFAS_START_DATE": "2020-06-01"})
    assert p.start_date == "2020-06-01"


def test_apply_config_start_date_blank_falls_back_to_default():
    p = TefasPlugin()
    p.apply_config({"TEFAS_START_DATE": ""})
    assert p.start_date == _DEFAULT_START


def test_init_defaults():
    p = TefasPlugin()
    assert p.funds == []
    assert p.start_date == _DEFAULT_START
    assert p.sink_influxdb is True


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_register_and_health():
    p = TefasPlugin()
    p.funds = ["AFA"]
    md = asyncio.run(p.register())
    assert md.name == "tefas"
    h = asyncio.run(p.health_check())
    assert h == {
        "healthy": True,
        "tefas_available": tefas_funds.TEFAS_AVAILABLE,
        "funds": ["AFA"],
        "influxdb_sink": False,  # no time-series handle bound
        "timeseries_bound": False,
    }


def test_lifecycle_status_transitions():
    p = TefasPlugin()
    assert p.status == "registered"
    asyncio.run(p.initialize())
    assert p.status == "ready"
    asyncio.run(p.shutdown())
    assert p.status == "shutdown"


# ── _fetch_sync parse ────────────────────────────────────────────────────────
def test_fetch_sync_parses_rows_and_skips_none_price(monkeypatch):
    p = TefasPlugin()
    rows = [
        {"date": "2021-01-05", "price": 1.5},
        {"date": "2021-01-06", "price": None},  # skipped
        {"date": "2021-01-07", "price": 2.5},
    ]
    monkeypatch.setattr(tefas_funds, "Crawler", _fake_crawler(_FakeDF(rows)))
    out = p._fetch_sync("YAC", date(2021, 1, 5), date(2021, 1, 7))
    assert [pr for _, pr in out] == [1.5, 2.5]
    t = int(datetime(2021, 1, 5, tzinfo=timezone.utc).timestamp())
    assert out[0][0] == t


def test_fetch_sync_parses_date_object_row(monkeypatch):
    # tefas-crawler yields a real date object here — the `d.date() if
    # hasattr(d, "date")` branch (vs. the ISO-string branch) must handle it.
    p = TefasPlugin()
    rows = [{"date": date(2024, 1, 1), "price": 10.5}]
    monkeypatch.setattr(tefas_funds, "Crawler", _fake_crawler(_FakeDF(rows)))
    out = p._fetch_sync("AFA", date(2024, 1, 1), date(2024, 1, 2))
    ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    assert out == [(ts, 10.5)]


def test_fetch_sync_parses_datetime_object_row_normalized_to_day(monkeypatch):
    # a datetime (with a time-of-day) must normalise to that day's 00:00 UTC.
    p = TefasPlugin()
    rows = [{"date": datetime(2024, 1, 4, 12, 30), "price": 7.0}]
    monkeypatch.setattr(tefas_funds, "Crawler", _fake_crawler(_FakeDF(rows)))
    out = p._fetch_sync("AFA", date(2024, 1, 1), date(2024, 1, 5))
    ts = int(datetime(2024, 1, 4, tzinfo=timezone.utc).timestamp())
    assert out == [(ts, 7.0)]


def test_fetch_sync_empty_on_fetch_error(monkeypatch):
    p = TefasPlugin()
    monkeypatch.setattr(tefas_funds, "Crawler", _fake_crawler(raise_on_fetch=True))
    assert p._fetch_sync("YAC", date(2021, 1, 5), date(2021, 1, 7)) == []


def test_fetch_sync_empty_on_parse_error(monkeypatch):
    p = TefasPlugin()
    monkeypatch.setattr(
        tefas_funds,
        "Crawler",
        _fake_crawler(_FakeDF([], raise_on_iter=True)),
    )
    assert p._fetch_sync("YAC", date(2021, 1, 5), date(2021, 1, 7)) == []


# ── _fetch_history gate ──────────────────────────────────────────────────────
def test_fetch_history_noop_when_tefas_unavailable(monkeypatch):
    p = TefasPlugin()
    monkeypatch.setattr(tefas_funds, "TEFAS_AVAILABLE", False)
    out = asyncio.run(p._fetch_history("YAC", date(2021, 1, 5), date(2021, 1, 7)))
    assert out == []


def test_fetch_history_delegates_when_available(monkeypatch):
    p = TefasPlugin()
    monkeypatch.setattr(tefas_funds, "TEFAS_AVAILABLE", True)
    monkeypatch.setattr(
        tefas_funds,
        "Crawler",
        _fake_crawler(_FakeDF([{"date": "2021-01-05", "price": 3.0}])),
    )
    out = asyncio.run(p._fetch_history("YAC", date(2021, 1, 5), date(2021, 1, 7)))
    assert [pr for _, pr in out] == [3.0]


# ── get_fund_price ───────────────────────────────────────────────────────────
def test_get_fund_price_requires_code():
    assert asyncio.run(TefasPlugin().get_fund_price("")) == {
        "error": "code is required"
    }


def test_get_fund_price_uppercases_and_returns_latest(monkeypatch):
    p = TefasPlugin()
    captured = {}

    async def _fake_hist(code, start, end):
        captured["code"] = code
        return [(1, 10.0), (2, 11.0)]

    monkeypatch.setattr(p, "_fetch_history", _fake_hist)
    res = asyncio.run(p.get_fund_price("yac"))
    assert captured["code"] == "YAC"  # upper-cased
    assert res == {"code": "YAC", "price": 11.0}  # latest point


def test_get_fund_price_unavailable_when_empty(monkeypatch):
    p = TefasPlugin()

    async def _empty(code, start, end):
        return []

    monkeypatch.setattr(p, "_fetch_history", _empty)
    assert asyncio.run(p.get_fund_price("yac")) == {
        "code": "YAC",
        "error": "price unavailable",
    }


def test_fetch_sync_skips_nan_price(monkeypatch):
    # pandas NaN for a gap day is a float (not None) — must be skipped, or it
    # becomes `price=nan` and 400s the whole batch (stalling the resume).
    p = TefasPlugin()
    rows = [
        {"date": "2021-01-05", "price": 1.5},
        {"date": "2021-01-06", "price": float("nan")},  # skipped
        {"date": "2021-01-07", "price": 2.5},
    ]
    monkeypatch.setattr(tefas_funds, "Crawler", _fake_crawler(_FakeDF(rows)))
    out = p._fetch_sync("YAC", date(2021, 1, 5), date(2021, 1, 7))
    assert [pr for _, pr in out] == [1.5, 2.5]


# ── time series: the platform handle (bind_timeseries), no raw credentials ────
def test_declares_its_measurement_and_holds_no_influx_credentials():
    p = TefasPlugin({"influxdb": {"enabled": True, "token": "t"}})
    assert p.TIMESERIES == {"measurements": ["tefas_fund_price"]}
    assert not hasattr(p, "_influx_cfg")
    # Without a bound handle nothing is read or written.
    assert asyncio.run(p._latest_influx_date("AFA")) is None
    assert asyncio.run(p._write_history("AFA", [(1, 1.0)])) == 0


def test_sink_off_skips_the_handle(bind_timeseries):
    p = TefasPlugin()
    ts = bind_timeseries(p)
    p.sink_influxdb = False
    assert asyncio.run(p._latest_influx_date("AFA")) is None
    assert asyncio.run(p._write_history("AFA", [(1, 1.0)])) == 0
    assert ts.batches == [] and ts.latest_calls == []


def test_write_history_through_the_handle_in_chunks(bind_timeseries):
    p = TefasPlugin()
    ts = bind_timeseries(p)
    assert asyncio.run(p._write_history("AFA", [])) == 0
    assert asyncio.run(p._write_history("AFA", [(100, 1.25)])) == 1
    assert ts.batches == [(["tefas_fund_price,code=AFA price=1.25 100"], "s")]
    points = [(i, 1.0) for i in range(1, 4502)]
    assert asyncio.run(p._write_history("AFA", points)) == 4501
    assert [len(b) for b, _ in ts.batches[1:]] == [2000, 2000, 501]


def test_write_history_fail_soft_and_unsafe_code(bind_timeseries):
    p = TefasPlugin()
    bind_timeseries(p, fail_write=RuntimeError("down"))
    assert asyncio.run(p._write_history("AFA", [(1, 1.0)])) == 0
    p2 = TefasPlugin()
    ts = bind_timeseries(p2)
    assert asyncio.run(p2._write_history("A,B", [(1, 1.0)])) == 0
    assert ts.batches == []


def test_latest_influx_date_reads_the_resume_point(bind_timeseries):
    p = TefasPlugin()
    ts = bind_timeseries(
        p, latest={("tefas_fund_price", "code", "AFA"): "2024-02-01T00:00:00"}
    )
    assert asyncio.run(p._latest_influx_date("AFA")) == date(2024, 2, 1)
    assert asyncio.run(p._latest_influx_date("A B")) is None
    assert ts.latest_calls == [("tefas_fund_price", "code", "AFA")]
    p2 = TefasPlugin()
    bind_timeseries(p2, fail_latest=RuntimeError("down"))
    assert asyncio.run(p2._latest_influx_date("AFA")) is None


# ── collect_data — resume-point vs configured-start branching ────────────────
def _today():
    return datetime.now(timezone.utc).date()


def test_collect_data_up_to_date_when_resume_past_today(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA"]
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=_today()))

    def boom(*a, **kw):
        raise AssertionError("_fetch_history should not be called")

    monkeypatch.setattr(p, "_fetch_history", boom)
    result = asyncio.run(p.collect_data())
    assert result["funds"]["AFA"] == {"written": 0, "up_to_date": True}


def test_collect_data_resumes_from_day_after_latest(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA"]
    latest = _today() - timedelta(days=3)
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=latest))
    seen = {}

    async def fake_fetch(code, start, end):
        seen["start"] = start
        return [(1, 9.0)]

    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=1))
    result = asyncio.run(p.collect_data())
    assert seen["start"] == latest + timedelta(days=1)
    assert result["funds"]["AFA"]["from"] == (latest + timedelta(days=1)).isoformat()
    assert result["funds"]["AFA"]["latest_price"] == 9.0


def test_collect_data_uses_configured_start_when_no_resume(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA"]
    p.start_date = "2020-01-01"
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    seen = {}

    async def fake_fetch(code, start, end):
        seen["start"] = start
        return []

    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))
    asyncio.run(p.collect_data())
    assert seen["start"] == date(2020, 1, 1)


def test_collect_data_falls_back_to_default_start_when_unparseable(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA"]
    p.start_date = "not-a-date"
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    seen = {}

    async def fake_fetch(code, start, end):
        seen["start"] = start
        return []

    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))
    asyncio.run(p.collect_data())
    assert seen["start"] == date.fromisoformat(_DEFAULT_START)


def test_collect_data_aggregates_funds_independently(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA", "AAK"]
    p.start_date = "2024-01-01"
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    fetch_results = {"AFA": [(1, 1.1)], "AAK": []}

    async def fake_fetch(code, start, end):
        return fetch_results[code]

    async def fake_write(code, points):
        return len(points)

    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    monkeypatch.setattr(p, "_write_history", fake_write)
    result = asyncio.run(p.collect_data())
    assert result["funds"]["AFA"]["written"] == 1
    assert result["funds"]["AFA"]["latest_price"] == 1.1
    assert result["funds"]["AAK"]["written"] == 0
    assert result["funds"]["AAK"]["latest_price"] is None
    assert p._last == result


# ── analyze / refresh ────────────────────────────────────────────────────────
def test_analyze_before_any_collection():
    p = TefasPlugin()
    p.funds = ["AFA"]
    out = asyncio.run(p.analyze())
    assert out == {"message": "no data collected yet", "funds": ["AFA"]}


def test_analyze_returns_last_collection(monkeypatch):
    p = TefasPlugin()
    p.funds = ["AFA"]
    monkeypatch.setattr(p, "_latest_influx_date", AsyncMock(return_value=None))
    monkeypatch.setattr(p, "_fetch_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(p, "_write_history", AsyncMock(return_value=0))
    asyncio.run(p.collect_data())
    assert asyncio.run(p.analyze()) == p._last


def test_refresh_delegates_to_collect_data(monkeypatch):
    p = TefasPlugin()
    monkeypatch.setattr(p, "collect_data", AsyncMock(return_value={"ok": True}))
    assert asyncio.run(p.refresh()) == {"ok": True}


def test_get_fund_price_uses_ten_day_window(monkeypatch):
    p = TefasPlugin()
    seen = {}

    async def fake_fetch(code, start, end):
        seen["start"] = start
        seen["end"] = end
        return [(1, 3.0)]

    monkeypatch.setattr(p, "_fetch_history", fake_fetch)
    asyncio.run(p.get_fund_price("afa"))
    assert (seen["end"] - seen["start"]).days == 10
    assert seen["end"] == _today()
