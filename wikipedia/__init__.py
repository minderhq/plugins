"""Wikipedia — on-demand article summaries (community catalog plugin).

A keyless **AI-tool-first** plugin (a "Talent"): exposes a ``wiki_summary`` tool
the LLM can call to ground itself in a factual Wikipedia extract ("summarise X",
"who is Y?"). Uses Wikipedia's public REST summary API — no key, no storage — so
it needs no config decision and works anywhere. Unlike the catalog's data-source
plugins it doesn't collect a time series; it's a pure request/response capability,
the shape the marketplace's "Talents" are meant to sell.

Config (all optional):
  WIKI_LANG        Wikipedia language subdomain (default "en", e.g. "tr", "de").
  WIKI_HTTP_TIMEOUT per-request timeout seconds (default 10, env-only).
"""

import logging
import os
import re
from typing import Dict, Optional
from urllib.parse import quote

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata

__all__ = ["WikipediaPlugin"]

logger = logging.getLogger("minder.plugin.wikipedia")

# Wikipedia language subdomains are lowercase letters (+ a few with '-', e.g.
# zh-yue); restrict to that so the configured lang can't inject into the host.
_SAFE_LANG = re.compile(r"^[a-z]+(-[a-z]+)?\Z")


class WikipediaPlugin(PluginBase):
    DISPLAY = {
        "label": "Wikipedia",
        "summary": "Look up a factual article summary — an ask-Wikipedia tool for the LLM.",
        "logo": "book-open",
        "color": "#636466",
        "category": "ai-tool",
    }

    # Pure request/response — no storage backend needed.
    REQUIRES: Dict[str, list] = {"services": [], "optional_services": [], "bundles": []}

    ACTIONS = frozenset({"wiki_summary"})
    READ_ONLY_ACTIONS = frozenset({"wiki_summary"})

    CONFIG_SCHEMA = [
        {
            "key": "WIKI_LANG",
            "type": "string",
            "default": "en",
            "description": "Wikipedia language subdomain (e.g. 'en', 'tr', 'de').",
            "widget": "text",
            "group": "Lookup",
        },
    ]

    AI_TOOLS = [
        {
            "name": "wiki_summary",
            "description": (
                "Look up a short factual summary of a topic from Wikipedia "
                "(title, extract, and the article URL)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "What to look up, e.g. 'Alan Turing', 'RAG'.",
                    },
                },
                "required": ["topic"],
            },
            "action": "wiki_summary",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(os.environ.get("WIKI_HTTP_TIMEOUT", "10"))  # env-only
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "WIKI_LANG" in cfg:
            lang = str(cfg["WIKI_LANG"] or "en").strip().lower()
            self.lang = lang if _SAFE_LANG.match(lang) else "en"

    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="wikipedia",
            version="1.0.0",
            description="Keyless Wikipedia article-summary lookup, exposed as an AI tool.",
            author="minderhq",
            capabilities=["analyze", "wikipedia"],
            data_sources=["wikipedia"],
        )

    # ── the tool ──────────────────────────────────────────────────────────────
    async def wiki_summary(self, topic: str) -> Dict:
        """Return {title, extract, url} for a topic, or an error marker. Never
        raises — a miss/unreachable API degrades to an ``error`` field."""
        if not topic or not topic.strip():
            return {"error": "topic is required"}
        title = quote(topic.strip().replace(" ", "_"), safe="")
        url = f"https://{self.lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout,
                follow_redirects=True,
                headers={"User-Agent": "minder-wikipedia-plugin"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.json() or {}
        except Exception as e:
            logger.warning(
                f"⚠️ Wikipedia lookup failed for {topic!r}: {type(e).__name__}"
            )
            return {"topic": topic, "error": "lookup unavailable"}
        extract = body.get("extract")
        if not extract:
            return {"topic": topic, "error": "no summary found"}
        page = (body.get("content_urls") or {}).get("desktop") or {}
        return {
            "title": body.get("title", topic),
            "extract": extract,
            "url": page.get("page", ""),
        }
