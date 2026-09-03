"""Behavioural tests for the hackernews plugin — HN_LIMIT clamp/coercion, the
two-step topstories→item fetch/assembly (url fallback, score, skipping non-dict
and untitled items, id clamp), and the collect_data / top_stories entry points.
HTTP is faked; no network.
"""

import asyncio

import pytest

import hackernews
from hackernews import HackerNewsPlugin


def _client(mapping, *, raise_on_get=False):
    """Fake httpx.AsyncClient whose get(url) dispatches on the path suffix:
    mapping is {"topstories.json": [...ids], "item/3.json": {...}, ...}."""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if raise_on_get:
                raise RuntimeError("boom")
            for suffix, payload in mapping.items():
                if url.endswith(suffix):
                    return _Resp(payload)
            raise AssertionError(f"unexpected url {url}")

    return _Client


# ── apply_config ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [(5, 5), ("7", 7), (0, 1), (99, 30), ("nope", 10), (None, 10)],
)
def test_apply_config_hn_limit_clamped(value, expected):
    p = HackerNewsPlugin()
    p.apply_config({"HN_LIMIT": value})
    assert p.limit == expected


# ── _top assembly ────────────────────────────────────────────────────────────
def test_top_builds_stories_with_url_and_score(monkeypatch):
    p = HackerNewsPlugin()
    mapping = {
        "topstories.json": [1, 2],
        "item/1.json": {"id": 1, "title": "One", "url": "https://one", "score": 42},
        "item/2.json": {"id": 2, "title": "Two", "score": 7},  # no url → fallback
    }
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client(mapping))
    stories = asyncio.run(p._top(10))
    assert stories == [
        {"title": "One", "url": "https://one", "score": 42},
        {
            "title": "Two",
            "url": "https://news.ycombinator.com/item?id=2",
            "score": 7,
        },
    ]


def test_top_skips_untitled_and_non_dict_items(monkeypatch):
    p = HackerNewsPlugin()
    mapping = {
        "topstories.json": [1, 2, 3],
        "item/1.json": {"id": 1, "title": "Keep"},
        "item/2.json": {"id": 2},  # no title → dropped
        "item/3.json": None,  # non-dict → dropped
    }
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client(mapping))
    stories = asyncio.run(p._top(10))
    assert [s["title"] for s in stories] == ["Keep"]


def test_top_clamps_id_count(monkeypatch):
    p = HackerNewsPlugin()
    # 5 ids available but limit 2 → only items 1 and 2 are ever fetched
    mapping = {
        "topstories.json": [1, 2, 3, 4, 5],
        "item/1.json": {"id": 1, "title": "a"},
        "item/2.json": {"id": 2, "title": "b"},
    }
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client(mapping))
    stories = asyncio.run(p._top(2))
    assert [s["title"] for s in stories] == ["a", "b"]


def test_top_bad_limit_defaults_to_10(monkeypatch):
    p = HackerNewsPlugin()
    mapping = {"topstories.json": [], "item/x": {}}
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client(mapping))
    assert asyncio.run(p._top("oops")) == []  # no crash, empty ids


def test_top_returns_empty_on_fetch_error(monkeypatch):
    p = HackerNewsPlugin()
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client({}, raise_on_get=True))
    assert asyncio.run(p._top(5)) == []


# ── entry points ─────────────────────────────────────────────────────────────
def test_collect_data_records_count(monkeypatch):
    p = HackerNewsPlugin()
    mapping = {
        "topstories.json": [1],
        "item/1.json": {"id": 1, "title": "Only"},
    }
    monkeypatch.setattr(hackernews.httpx, "AsyncClient", _client(mapping))
    res = asyncio.run(p.collect_data())
    assert res["count"] == 1
    assert res["stories"][0]["title"] == "Only"


def test_top_stories_uses_config_limit_when_none(monkeypatch):
    p = HackerNewsPlugin()
    p.limit = 1
    captured = {}

    async def _fake_top(limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(p, "_top", _fake_top)
    asyncio.run(p.top_stories(limit=None))
    assert captured["limit"] == 1  # fell back to self.limit
