"""Behavioural tests for the news plugin — beyond the shared contract/lifecycle
checks in test_catalog.py, these exercise the plugin's real parsing and action
logic: feed-spec parsing, config coercion, RSS/Atom extraction, the SSRF gate,
and the get_news / collect_data entry points. All HTTP is faked; no network.
"""

import asyncio
from types import SimpleNamespace

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


# ── _is_safe_feed_url: the real SSRF classification (#370) ────────────────────
# Every other test stubs _is_safe_feed_url wholesale (see _plugin), so the guard's
# OWN logic -- https-only + rejecting any host that resolves to a private/loopback/
# link-local/reserved/multicast address -- was never actually exercised. A silent
# regression (e.g. dropping the is_link_local check, reopening the cloud-metadata
# 169.254.169.254 SSRF) would have passed CI. These pin the classifier directly.


def _resolving_to(monkeypatch, ips):
    """Stub the event loop's getaddrinfo so _is_safe_feed_url sees `ips` (a list
    of IP strings) for any hostname. Pass the OSError class to simulate a
    resolution failure. Mirrors getaddrinfo's 5-tuple shape (sockaddr at [4],
    ip at [4][0]), which is all _is_safe_feed_url reads."""

    class _FakeLoop:
        async def getaddrinfo(self, host, port):
            if ips is OSError:
                raise OSError("name resolution failed")
            return [(2, 1, 6, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(news.asyncio, "get_running_loop", lambda: _FakeLoop())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/feed",  # not https
        "https://",  # no hostname
        "ftp://example.com/feed",  # wrong scheme entirely
    ],
)
def test_is_safe_feed_url_rejects_non_https_or_hostless(monkeypatch, url):
    # would resolve to a public address if it ever got that far -- scheme/host
    # gate must reject it before any DNS lookup.
    _resolving_to(monkeypatch, ["93.184.216.34"])
    assert asyncio.run(news._is_safe_feed_url(url)) is False


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private (RFC1918)
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local -- the cloud-metadata endpoint
        "240.0.0.1",  # reserved
        "224.0.0.1",  # multicast
    ],
)
def test_is_safe_feed_url_rejects_internal_addresses(monkeypatch, ip):
    _resolving_to(monkeypatch, [ip])
    assert asyncio.run(news._is_safe_feed_url("https://feed.example/rss")) is False


def test_is_safe_feed_url_allows_public_https(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    assert asyncio.run(news._is_safe_feed_url("https://example.com/rss")) is True


def test_is_safe_feed_url_rejects_when_any_record_is_internal(monkeypatch):
    # A hostname resolving to BOTH a public and a private address must be
    # rejected -- the guard checks every A-record, not just the first, so a
    # DNS-rebinding-style "one public, one internal" answer can't slip through.
    _resolving_to(monkeypatch, ["93.184.216.34", "10.1.2.3"])
    assert asyncio.run(news._is_safe_feed_url("https://dual.example/rss")) is False


def test_is_safe_feed_url_rejects_on_resolution_failure(monkeypatch):
    # getaddrinfo raising (NXDOMAIN / no network) must fail closed, not open.
    _resolving_to(monkeypatch, OSError)
    assert asyncio.run(news._is_safe_feed_url("https://nxdomain.example/rss")) is False


# ── _reject_unsafe_redirect: the follow-redirect-into-SSRF hook (#370) ────────
# _is_safe_feed_url only vets the ORIGINAL feed URL; a public host that 302s to
# 169.254.169.254 would reopen the SSRF one hop later. This event hook re-vets
# every redirect target. It had zero coverage.


def _redirect_resp(*, is_redirect, location, request_url="https://feed.example/rss"):
    return SimpleNamespace(
        is_redirect=is_redirect,
        headers=({"location": location} if location is not None else {}),
        request=SimpleNamespace(url=news.httpx.URL(request_url)),
    )


def test_reject_unsafe_redirect_passes_non_redirect(monkeypatch):
    # A normal (non-redirect) response must never be probed or raise.
    called = []

    async def _spy(url):
        called.append(url)
        return True

    monkeypatch.setattr(news, "_is_safe_feed_url", _spy)
    asyncio.run(
        news._reject_unsafe_redirect(_redirect_resp(is_redirect=False, location=None))
    )
    assert called == []  # short-circuited before the safety check


def test_reject_unsafe_redirect_ignores_redirect_without_location(monkeypatch):
    # A redirect with no Location header is a no-op (httpx won't follow it anyway).
    asyncio.run(
        news._reject_unsafe_redirect(_redirect_resp(is_redirect=True, location=None))
    )


def test_reject_unsafe_redirect_raises_on_unsafe_target(monkeypatch):
    async def _unsafe(url):
        return False

    monkeypatch.setattr(news, "_is_safe_feed_url", _unsafe)
    resp = _redirect_resp(
        is_redirect=True, location="https://169.254.169.254/latest/meta-data/"
    )
    with pytest.raises(news.httpx.HTTPError):
        asyncio.run(news._reject_unsafe_redirect(resp))


def test_reject_unsafe_redirect_allows_safe_target(monkeypatch):
    async def _safe(url):
        return True

    monkeypatch.setattr(news, "_is_safe_feed_url", _safe)
    resp = _redirect_resp(is_redirect=True, location="https://cdn.example/rss")
    asyncio.run(news._reject_unsafe_redirect(resp))  # must not raise


def test_reject_unsafe_redirect_resolves_relative_location(monkeypatch):
    # A relative Location must be joined against the request URL before the
    # safety check -- not passed through bare (which would vet the wrong thing).
    seen = {}

    async def _spy(url):
        seen["url"] = url
        return True

    monkeypatch.setattr(news, "_is_safe_feed_url", _spy)
    resp = _redirect_resp(
        is_redirect=True, location="/elsewhere", request_url="https://feed.example/rss"
    )
    asyncio.run(news._reject_unsafe_redirect(resp))
    assert seen["url"] == "https://feed.example/elsewhere"
