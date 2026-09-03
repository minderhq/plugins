"""Behavioural tests for the wikipedia plugin — WIKI_LANG safe-coercion and the
wiki_summary tool (required topic, URL encoding + language, success mapping, and
the fail-soft no-summary / unreachable paths). HTTP is faked; no network.
"""

import asyncio

import pytest

import wikipedia
from wikipedia import WikipediaPlugin


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client(payload, *, raise_on_get=False, capture=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if capture is not None:
                capture["url"] = url
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(payload)

    return _Client


_SUMMARY = {
    "title": "Alan Turing",
    "extract": "Alan Turing was a mathematician.",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Alan_Turing"}},
}


# ── apply_config (safe lang) ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [("tr", "tr"), ("EN", "en"), ("zh-yue", "zh-yue"), ("", "en"), ("../evil", "en")],
)
def test_apply_config_lang_coercion(value, expected):
    p = WikipediaPlugin()
    p.apply_config({"WIKI_LANG": value})
    assert p.lang == expected


# ── wiki_summary ─────────────────────────────────────────────────────────────
def test_wiki_summary_requires_topic():
    assert asyncio.run(WikipediaPlugin().wiki_summary("")) == {
        "error": "topic is required"
    }
    assert asyncio.run(WikipediaPlugin().wiki_summary("   ")) == {
        "error": "topic is required"
    }


def test_wiki_summary_success_maps_fields(monkeypatch):
    p = WikipediaPlugin()
    monkeypatch.setattr(wikipedia.httpx, "AsyncClient", _client(_SUMMARY))
    assert asyncio.run(p.wiki_summary("Alan Turing")) == {
        "title": "Alan Turing",
        "extract": "Alan Turing was a mathematician.",
        "url": "https://en.wikipedia.org/wiki/Alan_Turing",
    }


def test_wiki_summary_encodes_topic_and_language(monkeypatch):
    p = WikipediaPlugin()
    p.lang = "tr"
    cap = {}
    monkeypatch.setattr(wikipedia.httpx, "AsyncClient", _client(_SUMMARY, capture=cap))
    asyncio.run(p.wiki_summary("Ada Lovelace"))
    # spaces → underscores, then percent-encoded; language subdomain honoured
    assert cap["url"] == (
        "https://tr.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace"
    )


def test_wiki_summary_no_extract_is_a_miss(monkeypatch):
    p = WikipediaPlugin()
    monkeypatch.setattr(wikipedia.httpx, "AsyncClient", _client({"title": "X"}))
    assert asyncio.run(p.wiki_summary("Nowhere")) == {
        "topic": "Nowhere",
        "error": "no summary found",
    }


def test_wiki_summary_unreachable_is_fail_soft(monkeypatch):
    p = WikipediaPlugin()
    monkeypatch.setattr(wikipedia.httpx, "AsyncClient", _client({}, raise_on_get=True))
    assert asyncio.run(p.wiki_summary("Anything")) == {
        "topic": "Anything",
        "error": "lookup unavailable",
    }


def test_wiki_summary_missing_url_defaults_to_empty(monkeypatch):
    p = WikipediaPlugin()
    payload = {"title": "T", "extract": "some text"}  # no content_urls
    monkeypatch.setattr(wikipedia.httpx, "AsyncClient", _client(payload))
    res = asyncio.run(p.wiki_summary("T"))
    assert res["extract"] == "some text" and res["url"] == ""
