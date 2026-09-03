"""Behavioural tests for the tefas_funds plugin — fund-code/config coercion, the
blocking tefas-crawler parse (date coercion, None-price skip, fail-soft on fetch
and parse errors), the TEFAS_AVAILABLE gate, and get_fund_price. The Crawler is
faked; no network and no real tefas-crawler needed.
"""

import asyncio
from datetime import date, datetime, timezone

import pytest

import tefas_funds
from tefas_funds import TefasPlugin


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
