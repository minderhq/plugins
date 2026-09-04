"""Behavioural tests for the arxiv plugin — ARXIV_MAX_RESULTS clamp, the
search_papers tool (required query, Atom-feed parsing + field mapping, the
max_results override, and the fail-soft unreachable / malformed-feed paths).
HTTP is faked; no network.
"""

import asyncio

import pytest

import arxiv
from arxiv import ArxivPlugin

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <published>2024-01-02T00:00:00Z</published>
    <title>Retrieval-Augmented
      Generation for Knowledge</title>
    <summary>  A method combining retrieval with generation.  </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <published>2024-01-01T00:00:00Z</published>
    <title>Graph Neural Networks</title>
    <summary>On learning over graphs.</summary>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def _client(text, *, raise_on_get=False, capture=None):
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
                capture["params"] = params
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(text)

    return _Client


# ── apply_config (clamp) ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [(3, 3), ("7", 7), (0, 1), (999, 20), (-4, 1), ("x", 5)],
)
def test_apply_config_clamps_max_results(value, expected):
    p = ArxivPlugin()
    p.apply_config({"ARXIV_MAX_RESULTS": value})
    assert p.max_results == expected


# ── search_papers ────────────────────────────────────────────────────────────
def test_search_papers_requires_query():
    assert asyncio.run(ArxivPlugin().search_papers("")) == {
        "error": "query is required"
    }
    assert asyncio.run(ArxivPlugin().search_papers("   ")) == {
        "error": "query is required"
    }


def test_search_papers_parses_and_maps_the_feed(monkeypatch):
    cap = {}
    monkeypatch.setattr(arxiv.httpx, "AsyncClient", _client(_FEED, capture=cap))
    out = asyncio.run(ArxivPlugin().search_papers("RAG"))
    assert out["query"] == "RAG" and out["count"] == 2
    first = out["papers"][0]
    # whitespace in the title is collapsed; the abstract is trimmed + squashed
    assert first["title"] == "Retrieval-Augmented Generation for Knowledge"
    assert first["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert first["summary"] == "A method combining retrieval with generation."
    assert first["published"] == "2024-01-02"
    assert first["url"] == "http://arxiv.org/abs/2401.00001v1"
    # newest-first sort is requested + the query is prefixed with all:
    assert cap["params"]["search_query"] == "all:RAG"
    assert cap["params"]["sortBy"] == "submittedDate"


def test_search_papers_max_results_override_is_clamped(monkeypatch):
    cap = {}
    monkeypatch.setattr(arxiv.httpx, "AsyncClient", _client(_FEED, capture=cap))
    asyncio.run(ArxivPlugin().search_papers("RAG", max_results=99))
    assert cap["params"]["max_results"] == "20"  # clamped to the cap (string param)


def test_search_papers_unreachable_is_fail_soft(monkeypatch):
    monkeypatch.setattr(arxiv.httpx, "AsyncClient", _client("", raise_on_get=True))
    out = asyncio.run(ArxivPlugin().search_papers("RAG"))
    assert out == {"query": "RAG", "error": "search unavailable"}


def test_search_papers_malformed_feed_yields_no_papers(monkeypatch):
    monkeypatch.setattr(arxiv.httpx, "AsyncClient", _client("not xml <<<"))
    out = asyncio.run(ArxivPlugin().search_papers("RAG"))
    assert out["count"] == 0 and out["papers"] == []
