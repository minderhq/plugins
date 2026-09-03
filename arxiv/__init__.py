"""arXiv — on-demand academic paper search (community catalog plugin).

A keyless **AI-tool-first** plugin (a "Talent"): exposes a ``search_papers`` tool
the LLM can call to ground itself in recent research ("find papers on retrieval-
augmented generation", "latest work on graph neural networks"). Uses arXiv's
public Atom query API — no key, no storage — so it needs no config decision and
works anywhere. Like the ``wikipedia`` Talent it's a pure request/response
capability (no time series), the shape the marketplace's "Talents" sell; it fits
Minder's knowledge/RAG brand especially well.

Config (all optional):
  ARXIV_MAX_RESULTS   default number of papers to return (1-20, default 5).
  ARXIV_HTTP_TIMEOUT  per-request timeout seconds (default 12, env-only).
"""

import logging
import os
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata

__all__ = ["ArxivPlugin"]

logger = logging.getLogger("minder.plugin.arxiv")

# HTTPS (arXiv supports it) — the query API returns an Atom 1.0 feed.
_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_MAX_CAP = 20


def _clamp(value: object, default: int = 5) -> int:
    """Coerce a caller/config-supplied count into 1.._MAX_CAP (fail-soft)."""
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_CAP))


class ArxivPlugin(PluginBase):
    DISPLAY = {
        "label": "arXiv",
        "summary": "Search academic papers on arXiv — a research-lookup tool for the LLM.",
        "logo": "graduation-cap",
        "color": "#b31b1b",
        "category": "ai-tool",
    }

    # Pure request/response — no storage backend needed.
    REQUIRES: Dict[str, list] = {"services": [], "optional_services": [], "bundles": []}

    ACTIONS = frozenset({"search_papers"})
    READ_ONLY_ACTIONS = frozenset({"search_papers"})

    CONFIG_SCHEMA = [
        {
            "key": "ARXIV_MAX_RESULTS",
            "type": "int",
            "default": 5,
            "description": "Default number of papers to return (1-20).",
            "widget": "number",
            "group": "Search",
        },
    ]

    AI_TOOLS = [
        {
            "name": "search_papers",
            "description": (
                "Search arXiv for academic papers on a topic. Returns each "
                "paper's title, authors, abstract, publication date, and URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The research topic or keywords, e.g. "
                            "'retrieval augmented generation'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many papers to return (1-20).",
                    },
                },
                "required": ["query"],
            },
            "action": "search_papers",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(
            os.environ.get("ARXIV_HTTP_TIMEOUT", "12")
        )  # env-only
        self.max_results = 5
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "ARXIV_MAX_RESULTS" in cfg:
            self.max_results = _clamp(cfg["ARXIV_MAX_RESULTS"])

    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="arxiv",
            version="1.0.0",
            description="Keyless arXiv academic-paper search, exposed as an AI tool.",
            author="minderhq",
            capabilities=["analyze", "arxiv", "research"],
            data_sources=["arxiv"],
        )

    # ── the tool ──────────────────────────────────────────────────────────────
    async def search_papers(
        self, query: str, max_results: Optional[int] = None
    ) -> Dict:
        """Return {query, count, papers[]} for a topic, or an error marker. Never
        raises — a miss/unreachable API degrades to an ``error`` field."""
        if not query or not query.strip():
            return {"error": "query is required"}
        n = (
            _clamp(max_results, self.max_results)
            if max_results is not None
            else self.max_results
        )
        # all-string params (arXiv treats them as strings anyway) so the httpx
        # QueryParams mapping stays homogeneously typed.
        params = {
            "search_query": f"all:{query.strip()}",
            "start": "0",
            "max_results": str(n),
            # newest first is the most useful default for "latest work on X".
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout,
                headers={"User-Agent": "minder-arxiv-plugin"},
            ) as client:
                resp = await client.get(_API, params=params)
                resp.raise_for_status()
                feed = resp.text
        except Exception as e:
            logger.warning(f"⚠️ arXiv search failed for {query!r}: {type(e).__name__}")
            return {"query": query, "error": "search unavailable"}
        papers = _parse_feed(feed)
        return {"query": query, "count": len(papers), "papers": papers}


def _parse_feed(feed_text: str) -> List[Dict]:
    """Parse an arXiv Atom feed into a list of paper dicts (fail-soft: a malformed
    feed yields []). stdlib ElementTree does not resolve external entities, and
    the feed is fetched over HTTPS, so this is safe against XXE for our use."""
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError:
        return []
    papers: List[Dict] = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
        summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
        published = (entry.findtext(f"{_ATOM}published") or "").strip()[:10]
        url = (entry.findtext(f"{_ATOM}id") or "").strip()
        authors = [
            (a.findtext(f"{_ATOM}name") or "").strip()
            for a in entry.findall(f"{_ATOM}author")
        ]
        papers.append(
            {
                "title": title,
                "authors": [a for a in authors if a],
                "summary": summary[:600],
                "published": published,
                "url": url,
            }
        )
    return papers
