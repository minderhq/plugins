"""Hacker News — top stories (community catalog plugin).

Polls the public, **keyless** Hacker News Firebase API for the current top
stories and exposes a ``top_stories`` AI tool the LLM can call. A second worked
catalog plugin (alongside frankfurter) demonstrating config + an AI tool with no
storage requirement.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata

__all__ = ["HackerNewsPlugin"]

logger = logging.getLogger("minder.plugin.hackernews")

_API = "https://hacker-news.firebaseio.com/v0"


class HackerNewsPlugin(PluginBase):
    DISPLAY = {
        "label": "Hacker News",
        "summary": "Current Hacker News top stories, plus an ask-for-headlines tool.",
        "logo": "newspaper",
        "color": "#ff6600",
        "category": "data-source",
    }

    # No storage needed — headlines are fetched on demand.
    REQUIRES = {"services": [], "optional_services": [], "bundles": []}

    ACTIONS = frozenset({"refresh", "top_stories"})
    READ_ONLY_ACTIONS = frozenset({"top_stories"})

    CONFIG_SCHEMA = [
        {
            "key": "HN_LIMIT",
            "type": "int",
            "default": 10,
            "description": "How many top stories to fetch.",
            "widget": "number",
            "min": 1,
            "max": 30,
            "group": "Feed",
        },
    ]

    AI_TOOLS = [
        {
            "name": "top_stories",
            "description": "Get the current top Hacker News stories (title + url).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many stories (1-30).",
                    },
                },
            },
            "action": "top_stories",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(os.environ.get("HN_HTTP_TIMEOUT", "10"))
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "HN_LIMIT" in cfg:
            try:
                self.limit = max(1, min(30, int(cfg["HN_LIMIT"])))
            except (TypeError, ValueError):
                self.limit = 10

    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="hackernews",
            version="1.0.0",
            description="Keyless Hacker News top stories + a headlines tool.",
            author="minderhq",
            capabilities=["collect", "analyze", "news"],
            data_sources=["hacker-news"],
        )

    async def _fetch_json(self, client: httpx.AsyncClient, path: str):
        resp = await client.get(f"{_API}/{path}")
        resp.raise_for_status()
        return resp.json()

    async def _top(self, limit: int) -> List[Dict]:
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                ids = await self._fetch_json(client, "topstories.json") or []
                ids = ids[: max(1, min(30, limit))]
                items = await asyncio.gather(
                    *[self._fetch_json(client, f"item/{i}.json") for i in ids],
                    return_exceptions=True,
                )
        except Exception as e:
            logger.warning(f"hn fetch failed: {type(e).__name__}: {e}")
            return []
        stories: List[Dict] = []
        for it in items:
            if isinstance(it, dict) and it.get("title"):
                stories.append(
                    {
                        "title": it["title"],
                        "url": it.get(
                            "url",
                            f"https://news.ycombinator.com/item?id={it.get('id')}",
                        ),
                        "score": it.get("score"),
                    }
                )
        return stories

    async def collect_data(self) -> Dict:
        stories = await self._top(self.limit)
        self._last = {
            "count": len(stories),
            "stories": stories,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"hn collect: {len(stories)} stories")
        return self._last

    async def refresh(self) -> Dict:
        """Force an immediate re-collection."""
        return await self.collect_data()

    async def top_stories(self, limit: Optional[int] = None) -> Dict:
        """Current top stories (backs the top_stories tool)."""
        n = self.limit if limit is None else limit
        return {"stories": await self._top(n)}
