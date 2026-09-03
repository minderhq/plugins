"""Open Library — on-demand book search (community catalog plugin).

A keyless **AI-tool-first** plugin (a "Talent"): exposes a ``search_books`` tool
the LLM can call to find books by title/topic/author ("books about distributed
systems", "novels by Ursula K. Le Guin"). Uses Open Library's public keyless
search API — no key, no storage. Rounds out the catalog's knowledge-lookup
Talents (``wikipedia`` for encyclopedia, ``arxiv`` for papers, this for books),
fitting Minder's knowledge/RAG brand.

Config (all optional):
  OPENLIBRARY_MAX_RESULTS   default number of books to return (1-20, default 5).
  OPENLIBRARY_HTTP_TIMEOUT  per-request timeout seconds (default 12, env-only).
"""

import logging
import os
from typing import Dict, List, Optional

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata

__all__ = ["OpenLibraryPlugin"]

logger = logging.getLogger("minder.plugin.openlibrary")

_API = "https://openlibrary.org/search.json"
_BASE = "https://openlibrary.org"
_MAX_CAP = 20


def _clamp(value: object, default: int = 5) -> int:
    """Coerce a caller/config-supplied count into 1.._MAX_CAP (fail-soft)."""
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_CAP))


class OpenLibraryPlugin(PluginBase):
    DISPLAY = {
        "label": "Open Library",
        "summary": "Search books on Open Library — a book-lookup tool for the LLM.",
        "logo": "library",
        "color": "#e1dcc5",
        "category": "ai-tool",
    }

    # Pure request/response — no storage backend needed.
    REQUIRES: Dict[str, list] = {"services": [], "optional_services": [], "bundles": []}

    ACTIONS = frozenset({"search_books"})
    READ_ONLY_ACTIONS = frozenset({"search_books"})

    CONFIG_SCHEMA = [
        {
            "key": "OPENLIBRARY_MAX_RESULTS",
            "type": "int",
            "default": 5,
            "description": "Default number of books to return (1-20).",
            "widget": "number",
            "group": "Search",
        },
    ]

    AI_TOOLS = [
        {
            "name": "search_books",
            "description": (
                "Search Open Library for books by title, topic, or author. "
                "Returns each book's title, authors, first publication year, "
                "and URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Title, topic, or author, e.g. "
                            "'distributed systems' or 'Ursula K. Le Guin'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many books to return (1-20).",
                    },
                },
                "required": ["query"],
            },
            "action": "search_books",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(
            os.environ.get("OPENLIBRARY_HTTP_TIMEOUT", "12")
        )  # env-only
        self.max_results = 5
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "OPENLIBRARY_MAX_RESULTS" in cfg:
            self.max_results = _clamp(cfg["OPENLIBRARY_MAX_RESULTS"])

    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="openlibrary",
            version="1.0.0",
            description="Keyless Open Library book search, exposed as an AI tool.",
            author="minderhq",
            capabilities=["analyze", "openlibrary", "books"],
            data_sources=["openlibrary"],
        )

    # ── the tool ──────────────────────────────────────────────────────────────
    async def search_books(self, query: str, max_results: Optional[int] = None) -> Dict:
        """Return {query, count, books[]} for a search, or an error marker. Never
        raises — a miss/unreachable API degrades to an ``error`` field."""
        if not query or not query.strip():
            return {"error": "query is required"}
        n = (
            _clamp(max_results, self.max_results)
            if max_results is not None
            else self.max_results
        )
        params = {
            "q": query.strip(),
            "limit": str(n),
            # only the fields we surface — keeps the (otherwise huge) response small.
            "fields": "title,author_name,first_publish_year,key",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout,
                headers={"User-Agent": "minder-openlibrary-plugin"},
            ) as client:
                resp = await client.get(_API, params=params)
                resp.raise_for_status()
                body = resp.json() or {}
        except Exception as e:
            logger.warning(
                f"⚠️ Open Library search failed for {query!r}: {type(e).__name__}"
            )
            return {"query": query, "error": "search unavailable"}
        books = _map_docs(body.get("docs") or [])
        return {"query": query, "count": len(books), "books": books}


def _map_docs(docs: List[Dict]) -> List[Dict]:
    """Map Open Library search ``docs`` to compact book records (skips non-dicts
    defensively — the API is trusted but the mapping stays fail-soft)."""
    books: List[Dict] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        key = str(d.get("key") or "")
        books.append(
            {
                "title": d.get("title", ""),
                "authors": [a for a in (d.get("author_name") or []) if a],
                "first_published": d.get("first_publish_year"),
                "url": f"{_BASE}{key}" if key.startswith("/") else key,
            }
        )
    return books
