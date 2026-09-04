"""Behavioural tests for the openlibrary plugin — OPENLIBRARY_MAX_RESULTS clamp,
the search_books tool (required query, docs mapping incl. URL from key + author
filtering, the max_results override, and the fail-soft unreachable path). HTTP is
faked; no network.
"""

import asyncio

import pytest

import openlibrary
from openlibrary import OpenLibraryPlugin

_BODY = {
    "numFound": 2,
    "docs": [
        {
            "title": "Designing Data-Intensive Applications",
            "author_name": ["Martin Kleppmann"],
            "first_publish_year": 2017,
            "key": "/works/OL17930368W",
        },
        {
            "title": "No Author Book",
            "author_name": None,  # missing authors -> filtered to []
            "first_publish_year": None,
            "key": "OL-no-slash",  # a key without a leading slash stays as-is
        },
    ],
}


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
                capture["params"] = params
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(payload)

    return _Client


# ── apply_config (clamp) ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [(3, 3), ("7", 7), (0, 1), (999, 20), (-4, 1), ("x", 5)],
)
def test_apply_config_clamps_max_results(value, expected):
    p = OpenLibraryPlugin()
    p.apply_config({"OPENLIBRARY_MAX_RESULTS": value})
    assert p.max_results == expected


# ── search_books ─────────────────────────────────────────────────────────────
def test_search_books_requires_query():
    assert asyncio.run(OpenLibraryPlugin().search_books("")) == {
        "error": "query is required"
    }
    assert asyncio.run(OpenLibraryPlugin().search_books("   ")) == {
        "error": "query is required"
    }


def test_search_books_maps_docs(monkeypatch):
    cap = {}
    monkeypatch.setattr(openlibrary.httpx, "AsyncClient", _client(_BODY, capture=cap))
    out = asyncio.run(OpenLibraryPlugin().search_books("data intensive"))
    assert out["query"] == "data intensive" and out["count"] == 2
    first = out["books"][0]
    assert first["title"] == "Designing Data-Intensive Applications"
    assert first["authors"] == ["Martin Kleppmann"]
    assert first["first_published"] == 2017
    assert first["url"] == "https://openlibrary.org/works/OL17930368W"
    # a leading-slash key is absolutised; missing authors -> []
    second = out["books"][1]
    assert second["authors"] == []
    assert second["url"] == "OL-no-slash"  # no leading slash -> left as-is
    # the query + only the compact field set are requested
    assert cap["params"]["q"] == "data intensive"
    assert cap["params"]["fields"] == "title,author_name,first_publish_year,key"


def test_search_books_max_results_override_is_clamped(monkeypatch):
    cap = {}
    monkeypatch.setattr(openlibrary.httpx, "AsyncClient", _client(_BODY, capture=cap))
    asyncio.run(OpenLibraryPlugin().search_books("x", max_results=99))
    assert cap["params"]["limit"] == "20"  # clamped to the cap (string param)


def test_search_books_unreachable_is_fail_soft(monkeypatch):
    monkeypatch.setattr(
        openlibrary.httpx, "AsyncClient", _client({}, raise_on_get=True)
    )
    out = asyncio.run(OpenLibraryPlugin().search_books("x"))
    assert out == {"query": "x", "error": "search unavailable"}


def test_search_books_empty_docs_yields_no_books(monkeypatch):
    monkeypatch.setattr(openlibrary.httpx, "AsyncClient", _client({"docs": []}))
    out = asyncio.run(OpenLibraryPlugin().search_books("x"))
    assert out["count"] == 0 and out["books"] == []
