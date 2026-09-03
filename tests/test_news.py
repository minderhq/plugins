"""Behavioural tests for the news plugin — beyond the shared contract/lifecycle
checks in test_catalog.py, these exercise the plugin's real parsing and action
logic: feed-spec parsing, config coercion, RSS/Atom extraction, the SSRF gate,
and the get_news / collect_data entry points. All HTTP is faked; no network.
"""

import asyncio

import pytest

import news
from news import NewsPlugin

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Chan</title>
  <item><title>First</title><link>https://e/1</link><pubDate>Mon</pubDate></item>
  <item><title>Second</title><link>https://e/2</link></item>
  <item><title></title><link>https://e/blank</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>AtomChan</title>
  <entry><title>A-one</title><link href="https://a/1"/><updated>2020</updated></entry>
  <entry><title>A-two</title><link href="https://a/2"/></entry>
</feed>"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def _fake_client_returning(text, *, raise_on_get=False):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(text)

    return _FakeClient


def _plugin(monkeypatch, *, safe=True):
    """A NewsPlugin with the SSRF DNS check stubbed (safe/unsafe) so parsing can
    be tested without real name resolution."""

    async def _check(url):
        return safe

    monkeypatch.setattr(news, "_is_safe_feed_url", _check)
    return NewsPlugin()


# ── _parse_feeds ─────────────────────────────────────────────────────────────
def test_parse_feeds_splits_name_and_url_once():
    # the url itself contains ':' (scheme) — partition(":") must split only once
    out = NewsPlugin._parse_feeds("bbc:https://x/rss, hn:https://y/z")
    assert out == [("bbc", "https://x/rss"), ("hn", "https://y/z")]


def test_parse_feeds_skips_malformed_entries():
    assert NewsPlugin._parse_feeds("nocolon, :https://x, name:, ok:https://z") == [
        ("ok", "https://z")
    ]


def test_parse_feeds_empty_spec():
    assert NewsPlugin._parse_feeds("") == []
    assert NewsPlugin._parse_feeds("   ") == []


# ── apply_config coercion ────────────────────────────────────────────────────
def test_apply_config_max_items_bad_value_falls_back_to_10(monkeypatch):
    p = _plugin(monkeypatch)
    p.apply_config({"NEWS_MAX_ITEMS": "not-an-int"})
    assert p.max_items == 10
    p.apply_config({"NEWS_MAX_ITEMS": "3"})
    assert p.max_items == 3


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("nope", False),
    ],
)
def test_apply_config_sink_bool_coercion(monkeypatch, value, expected):
    p = _plugin(monkeypatch)
    p.apply_config({"NEWS_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


# ── _fetch_feed parsing ──────────────────────────────────────────────────────
def test_fetch_feed_parses_rss_and_drops_untitled(monkeypatch):
    p = _plugin(monkeypatch)
    p.max_items = 10
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    items = asyncio.run(p._fetch_feed("https://feed"))
    assert [i["title"] for i in items] == ["First", "Second"]  # blank-title dropped
    assert items[0] == {"title": "First", "link": "https://e/1", "published": "Mon"}


def test_fetch_feed_caps_at_max_items(monkeypatch):
    p = _plugin(monkeypatch)
    p.max_items = 1
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    items = asyncio.run(p._fetch_feed("https://feed"))
    assert [i["title"] for i in items] == ["First"]


def test_fetch_feed_atom_fallback(monkeypatch):
    p = _plugin(monkeypatch)
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(ATOM))
    items = asyncio.run(p._fetch_feed("https://feed"))
    assert items[0] == {"title": "A-one", "link": "https://a/1", "published": "2020"}
    assert [i["link"] for i in items] == ["https://a/1", "https://a/2"]


def test_fetch_feed_rejects_unsafe_url(monkeypatch):
    p = _plugin(monkeypatch, safe=False)
    # even if the client would return a feed, the SSRF gate returns [] first
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    assert asyncio.run(p._fetch_feed("https://internal")) == []


def test_fetch_feed_swallows_fetch_errors(monkeypatch):
    p = _plugin(monkeypatch)
    monkeypatch.setattr(
        news.httpx, "AsyncClient", _fake_client_returning("", raise_on_get=True)
    )
    assert asyncio.run(p._fetch_feed("https://feed")) == []


def test_fetch_feed_swallows_bad_xml(monkeypatch):
    p = _plugin(monkeypatch)
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning("<not-xml"))
    assert asyncio.run(p._fetch_feed("https://feed")) == []


# ── get_news action ──────────────────────────────────────────────────────────
def test_get_news_unknown_feed_reports_available(monkeypatch):
    p = _plugin(monkeypatch)
    p.feeds = [("bbc", "https://x")]
    res = asyncio.run(p.get_news(feed="nope"))
    assert res["error"] == "unknown feed"
    assert res["available"] == ["bbc"]


def test_get_news_returns_titles_and_honours_limit(monkeypatch):
    p = _plugin(monkeypatch)
    p.feeds = [("bbc", "https://x")]
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    res = asyncio.run(p.get_news(feed="bbc", limit=1))
    assert res == {"headlines": {"bbc": ["First"]}}


def test_get_news_bad_limit_defaults(monkeypatch):
    p = _plugin(monkeypatch)
    p.feeds = [("bbc", "https://x")]
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    res = asyncio.run(p.get_news(feed="bbc", limit="oops"))
    assert res["headlines"]["bbc"] == ["First", "Second"]  # limit fell back to 5


# ── collect_data ─────────────────────────────────────────────────────────────
def test_collect_data_aggregates_counts_and_gates_influx(monkeypatch):
    p = _plugin(monkeypatch)
    p.feeds = [("bbc", "https://x"), ("hn", "https://y")]
    p.sink_influxdb = False  # no influx config → write must be gated off
    monkeypatch.setattr(news.httpx, "AsyncClient", _fake_client_returning(RSS))
    res = asyncio.run(p.collect_data())
    assert res["counts"] == {"bbc": 2, "hn": 2}
    assert res["influxdb_written"] is False
    assert set(res["headlines"]) == {"bbc", "hn"}
