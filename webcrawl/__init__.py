"""Web-crawl connector plugin (first-party module plugin).

The first **connector** in the catalog — the fetch-from-source half of
minderhq/minder#1499's "connector-based ingestion" direction, proven with the one
source that needs **no OAuth / no credential decision**: the public web.

Given a configured list of seed URLs (and an optional shallow, same-domain crawl
depth), it fetches each page, extracts readable text (stdlib ``html.parser`` — no
new dependency, no XML/sitemap parsing), and hands that content to Minder's
**existing** rag ingestion API (``POST /v1/knowledge-bases/{kb_id}/upload``) — the
exact same upload path a manual file drop takes. No bespoke ingestion code: a
connector's whole job is fetch → normalize-to-text → hand off to the pipeline every
other upload already flows through.

This is deliberately the *public-web* connector only. The OAuth sources named in
 #1499 (Notion / Confluence / Google Drive / Slack) each need a per-plugin secrets /
credential story that the issue **explicitly defers** — they are recommended as
follow-up children, not built here.

Read vs. write split (matches this platform's "actions are JWT-gated, reads are
not" contract):
  * ``collect_data`` / ``analyze`` — an unauthenticated, side-effect-free **preview**
    (crawl + extract, report per-page title/size). Safe to run on the hourly loop; it
    never uploads, so it can't silently duplicate documents in a KB.
  * ``ingest`` — the JWT-gated **write**: crawl + extract + upload each page into the
    target KB. Sync model (one-time pull vs. keeping KB docs fresh as the source
    changes) is an open #1499 question, so ingestion is an explicit on-demand action,
    not an automatic recurring push.

SECURITY (this fetches arbitrary configured URLs):
  * SSRF guard on **every** outbound URL (``_is_safe_url``) — http(s)-only scheme
    allowlist, then resolve the host and reject any answer that lands on a
    private / loopback / link-local (incl. the 169.254.169.254 cloud-metadata
    endpoint) / reserved / multicast / unspecified address. Every A/AAAA record is
    checked (a DNS-rebind "one public, one internal" answer is rejected), and it
    fails **closed** on resolution error.
  * Redirects are re-vetted per hop (``_reject_unsafe_redirect``) so a public host
    can't 302 into an internal address one hop later.
  * Bounded crawl: max pages, max depth, per-page byte cap (via Content-Length and a
    hard read cap), per-request timeout, and a politeness delay between fetches.
  * Same-registered-host only when following links; only ``<a href>`` links from
    fetched HTML are followed (no sitemap/XML parsing at all, so no XXE surface).

Uses only deps already in the plugin-registry image (``httpx`` + the stdlib).

Config (GET/PUT /v1/plugins/webcrawl/config — all API-editable, no restart):
  WEBCRAWL_KB_ID          target knowledge base id (required to ingest).
  WEBCRAWL_URLS           seed URLs, comma-separated. Each must be http(s) and
                          resolve to a public address (non-conforming are skipped).
  WEBCRAWL_RAG_URL        rag-pipeline base URL (default the in-cluster service).
  WEBCRAWL_SERVICE_TOKEN  X-Service-Token for the internal upload call (secret).
  WEBCRAWL_MAX_DEPTH      shallow same-domain crawl depth (0 = only the seeds).
  WEBCRAWL_MAX_PAGES      hard cap on total pages fetched per run.
  WEBCRAWL_MAX_BYTES      per-page download/processing cap (bytes).
  WEBCRAWL_RATE_LIMIT     politeness delay between fetches (seconds).
  WEBCRAWL_HTTP_TIMEOUT   per-request timeout (seconds; env-only knob).
"""

import asyncio
import ipaddress
import logging
import os
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from minder_plugin_sdk import PluginMetadata

__all__ = ["WebCrawlPlugin"]

logger = logging.getLogger("minder.plugin.webcrawl")

_ALLOWED_SCHEMES = frozenset({"http", "https"})


# ── SSRF guard (module-level so it is independently testable / stubbable) ─────
async def _is_safe_url(url: str) -> bool:
    """http(s)-only + reject any host that resolves to a private / loopback /
    link-local / reserved / multicast / unspecified address (RFC1918, 127/8,
    169.254/16 incl. the cloud-metadata endpoint, etc.).

    WEBCRAWL_URLS is JWT-gated config, but this platform's trust boundaries are
    loose (single-tenant / self-hosted), so a leaked token shouldn't be able to
    turn "crawl these public docs" into "probe internal services". Resolves the
    hostname (not just IP literals), checks EVERY returned record so a
    DNS-rebind-style dual answer can't slip an internal address through, and
    fails **closed** if resolution errors.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


async def _reject_unsafe_redirect(response: httpx.Response) -> None:
    """httpx ``response`` event hook, fired on every response incl. intermediate
    redirects. ``_is_safe_url`` only vets the ORIGINAL URL; without this a public
    host could 302 to 169.254.169.254 and httpx would transparently follow it,
    reopening the SSRF primitive one hop later. Raises to abort the request; the
    caller's try/except handles it exactly like a connection error."""
    if response.is_redirect:
        location = response.headers.get("location")
        if not location:
            return
        next_url = str(httpx.URL(response.request.url).join(location))
        if not await _is_safe_url(next_url):
            raise httpx.HTTPError(f"redirect to unsafe URL rejected: {next_url}")


class _HTMLTextExtractor(HTMLParser):
    """Minimal, dependency-free HTML → (title, readable text, links) extractor.

    Drops ``<script>`` / ``<style>`` / ``<template>`` / ``<noscript>`` content,
    collects the document title, visible text, and every ``<a href>`` (for the
    bounded same-domain crawl). Deliberately conservative — no JS, no CSS, no
    external entity handling — since it parses untrusted remote HTML."""

    _SKIP = frozenset({"script", "style", "template", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self._chunks: List[str] = []
        self.links: List[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


class WebCrawlPlugin:
    """Fetch configured public URLs, extract readable text, and ingest into a KB
    via Minder's existing rag upload API. Preview is a read; ingest is a write."""

    DISPLAY = {
        "label": "Web Crawl",
        "summary": (
            "Connector: crawl a configured list of public URLs and ingest the "
            "extracted text into a knowledge base."
        ),
        "logo": "globe",
        "color": "#2563eb",
        "category": "connector",
    }

    # Needs the rag bundle (its rag-pipeline is the upload target). No storage
    # backend is written directly — ingestion goes through the HTTP upload API.
    REQUIRES: Dict[str, List[str]] = {
        "services": [],
        "optional_services": [],
        "bundles": ["rag"],
    }

    # ``ingest`` writes to a KB → JWT-gated. ``refresh`` re-runs the read-only
    # preview and is therefore also exposed unauthenticated via GET.
    ACTIONS = frozenset({"ingest", "refresh"})
    READ_ONLY_ACTIONS = frozenset({"refresh"})

    _DEFAULT_RAG_URL = "http://minder-rag-pipeline:8004"

    CONFIG_SCHEMA = [
        {
            "key": "WEBCRAWL_KB_ID",
            "type": "string",
            "default": "",
            "description": "Target knowledge base id to ingest crawled pages into.",
            "widget": "text",
            "group": "Target",
        },
        {
            "key": "WEBCRAWL_URLS",
            "type": "string",
            "default": "",
            "description": (
                "Seed URLs to crawl, comma-separated. Each must be http(s) and "
                "resolve to a public address — others are skipped at fetch time."
            ),
            "widget": "textarea",
            "rows": 3,
            "group": "Source",
        },
        {
            "key": "WEBCRAWL_MAX_DEPTH",
            "type": "int",
            "default": 0,
            "description": "Shallow same-domain crawl depth (0 = only the seed URLs).",
            "widget": "number",
            "min": 0,
            "max": 3,
            "group": "Crawl bounds",
        },
        {
            "key": "WEBCRAWL_MAX_PAGES",
            "type": "int",
            "default": 20,
            "description": "Hard cap on total pages fetched per run.",
            "widget": "number",
            "min": 1,
            "max": 200,
            "group": "Crawl bounds",
        },
        {
            "key": "WEBCRAWL_MAX_BYTES",
            "type": "int",
            "default": 2_000_000,
            "description": "Per-page download/processing cap in bytes.",
            "widget": "number",
            "group": "Crawl bounds",
        },
        {
            "key": "WEBCRAWL_RATE_LIMIT",
            "type": "float",
            "default": 1.0,
            "description": "Politeness delay between fetches, in seconds.",
            "widget": "number",
            "step": 0.1,
            "group": "Crawl bounds",
        },
        {
            "key": "WEBCRAWL_RAG_URL",
            "type": "string",
            "default": _DEFAULT_RAG_URL,
            "description": "rag-pipeline base URL the upload call targets.",
            "widget": "text",
            "group": "Target",
        },
        {
            "key": "WEBCRAWL_SERVICE_TOKEN",
            "type": "string",
            "default": "",
            "description": (
                "X-Service-Token for the internal upload call (matches "
                "rag-pipeline's SERVICE_SYNC_TOKEN). Leave blank if the upload "
                "endpoint is reached with a user JWT instead."
            ),
            "secret": True,
            "widget": "secret",
            "group": "Target",
        },
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.http_timeout = float(os.environ.get("WEBCRAWL_HTTP_TIMEOUT", "15"))
        self.status = "registered"
        self._last: Dict[str, Any] = {}
        # Bootstrap schema-backed config from defaults+env via the single
        # config→state path; the registry re-applies persisted (API) overrides
        # right after load.
        self.apply_config(
            {
                "WEBCRAWL_KB_ID": os.environ.get("WEBCRAWL_KB_ID", ""),
                "WEBCRAWL_URLS": os.environ.get("WEBCRAWL_URLS", ""),
                "WEBCRAWL_MAX_DEPTH": os.environ.get("WEBCRAWL_MAX_DEPTH", "0"),
                "WEBCRAWL_MAX_PAGES": os.environ.get("WEBCRAWL_MAX_PAGES", "20"),
                "WEBCRAWL_MAX_BYTES": os.environ.get("WEBCRAWL_MAX_BYTES", "2000000"),
                "WEBCRAWL_RATE_LIMIT": os.environ.get("WEBCRAWL_RATE_LIMIT", "1.0"),
                "WEBCRAWL_RAG_URL": os.environ.get(
                    "WEBCRAWL_RAG_URL", self._DEFAULT_RAG_URL
                ),
                "WEBCRAWL_SERVICE_TOKEN": os.environ.get("WEBCRAWL_SERVICE_TOKEN", ""),
            }
        )

    # ── config ────────────────────────────────────────────────────────────────
    def apply_config(self, cfg: Dict[str, Any]) -> None:
        """Map centrally-managed config → runtime state (no restart). See CONFIG_SCHEMA."""
        if "WEBCRAWL_KB_ID" in cfg:
            self.kb_id = str(cfg["WEBCRAWL_KB_ID"] or "").strip()
        if "WEBCRAWL_URLS" in cfg:
            self.urls = self._parse_urls(str(cfg["WEBCRAWL_URLS"] or ""))
        if "WEBCRAWL_MAX_DEPTH" in cfg:
            self.max_depth = self._coerce_int(cfg["WEBCRAWL_MAX_DEPTH"], 0, lo=0, hi=3)
        if "WEBCRAWL_MAX_PAGES" in cfg:
            self.max_pages = self._coerce_int(
                cfg["WEBCRAWL_MAX_PAGES"], 20, lo=1, hi=200
            )
        if "WEBCRAWL_MAX_BYTES" in cfg:
            self.max_bytes = self._coerce_int(
                cfg["WEBCRAWL_MAX_BYTES"], 2_000_000, lo=1_000, hi=50_000_000
            )
        if "WEBCRAWL_RATE_LIMIT" in cfg:
            self.rate_limit = self._coerce_float(cfg["WEBCRAWL_RATE_LIMIT"], 1.0)
        if "WEBCRAWL_RAG_URL" in cfg:
            self.rag_url = str(cfg["WEBCRAWL_RAG_URL"] or self._DEFAULT_RAG_URL).rstrip(
                "/"
            )
        if "WEBCRAWL_SERVICE_TOKEN" in cfg:
            self.service_token = str(cfg["WEBCRAWL_SERVICE_TOKEN"] or "")

    @staticmethod
    def _parse_urls(spec: str) -> List[str]:
        out: List[str] = []
        for item in spec.replace("\n", ",").split(","):
            url = item.strip()
            if url and url not in out:
                out.append(url)
        return out

    @staticmethod
    def _coerce_int(value: Any, default: int, *, lo: int, hi: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(n, hi))

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return default
        return n if n >= 0 else default

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="webcrawl",
            version="1.0.0",
            description=(
                "Connector: crawls configured public URLs, extracts readable "
                "text, and ingests it into a knowledge base via the rag upload API."
            ),
            author="Minder <core@minder.local>",
            capabilities=["collect", "analyze", "connector", "ingest"],
            data_sources=["web"],
            databases=[],
        )

    async def initialize(self) -> None:
        self.status = "ready"

    async def health_check(self) -> Dict[str, Any]:
        # MUST return {"healthy": <bool>} — the monitoring loop reads health["healthy"].
        return {
            "healthy": True,
            "seed_urls": len(self.urls),
            "kb_id": self.kb_id or None,
            "max_depth": self.max_depth,
            "max_pages": self.max_pages,
        }

    async def shutdown(self) -> None:
        self.status = "shutdown"

    # ── fetching / extraction ─────────────────────────────────────────────────
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        """Fetch one URL and return its HTML body (bounded), or None on any error
        or if the URL fails the SSRF gate / isn't html / exceeds the byte cap."""
        if not await _is_safe_url(url):
            logger.warning(f"⚠️ webcrawl rejected unsafe URL (SSRF gate): {url}")
            return None
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:  # network / redirect-rejected / HTTP error
            logger.warning(f"⚠️ webcrawl fetch failed for {url}: {type(e).__name__}")
            return None
        ctype = resp.headers.get("content-type", "")
        if ctype and "html" not in ctype.lower() and "text" not in ctype.lower():
            logger.info(f"webcrawl skipped non-text content ({ctype}): {url}")
            return None
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > self.max_bytes:
            logger.warning(f"⚠️ webcrawl skipped oversized page ({clen}B): {url}")
            return None
        text = resp.text
        # Hard cap on processed size even when Content-Length is absent/lying.
        if len(text.encode("utf-8", "ignore")) > self.max_bytes:
            text = text.encode("utf-8", "ignore")[: self.max_bytes].decode(
                "utf-8", "ignore"
            )
        return text

    def _same_host_links(self, base_url: str, links: List[str]) -> List[str]:
        """Resolve + filter extracted links to same-host http(s) targets."""
        base_host = urlsplit(base_url).hostname
        out: List[str] = []
        seen: Set[str] = set()
        for href in links:
            if href.startswith(("mailto:", "javascript:", "tel:", "#")):
                continue
            resolved = urljoin(base_url, href)
            parts = urlsplit(resolved)
            if parts.scheme not in _ALLOWED_SCHEMES or parts.hostname != base_host:
                continue
            clean = resolved.split("#", 1)[0]
            if clean not in seen:
                seen.add(clean)
                out.append(clean)
        return out

    async def _crawl(self) -> List[Dict[str, Any]]:
        """Bounded BFS from the seed URLs. Returns [{url, title, text, chars}] —
        at most ``max_pages`` pages, following same-host links up to ``max_depth``.
        Extraction only; no ingestion."""
        results: List[Dict[str, Any]] = []
        visited: Set[str] = set()
        queue: Deque[Tuple[str, int]] = deque((u, 0) for u in self.urls)
        async with httpx.AsyncClient(
            timeout=self.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": "MinderWebCrawl/1.0 (+minder connector plugin)"},
            event_hooks={"response": [_reject_unsafe_redirect]},
        ) as client:
            while queue and len(results) < self.max_pages:
                url, depth = queue.popleft()
                canon = url.split("#", 1)[0]
                if canon in visited:
                    continue
                visited.add(canon)
                html = await self._fetch(client, canon)
                if self.rate_limit:
                    await asyncio.sleep(self.rate_limit)
                if html is None:
                    continue
                parser = _HTMLTextExtractor()
                try:
                    parser.feed(html)
                except Exception as e:
                    logger.warning(f"⚠️ webcrawl parse failed for {canon}: {e}")
                    continue
                text = parser.text()
                if not text.strip():
                    continue
                results.append(
                    {
                        "url": canon,
                        "title": parser.title or canon,
                        "text": text,
                        "chars": len(text),
                    }
                )
                if depth < self.max_depth:
                    for link in self._same_host_links(canon, parser.links):
                        if link not in visited:
                            queue.append((link, depth + 1))
        return results

    # ── ingestion handoff (the connector → existing rag upload API) ────────────
    async def _upload(
        self, client: httpx.AsyncClient, page: Dict[str, Any]
    ) -> Optional[str]:
        """Hand one extracted page to the KB via the existing upload endpoint
        (``POST /v1/knowledge-bases/{kb_id}/upload``, multipart). Auth is the
        internal X-Service-Token (rag-pipeline's SERVICE_SYNC_TOKEN) when set.
        Returns the created ``document_id`` or None on failure."""
        # Filename is metadata only — the extractor registry sniffs real content,
        # not the extension — but a .txt name + text/plain matches the TXT extractor.
        slug = urlsplit(page["url"]).path.strip("/").replace("/", "_") or "index"
        filename = f"{slug[:80]}.txt"
        body = f"# {page['title']}\nSource: {page['url']}\n\n{page['text']}"
        headers = {}
        if self.service_token:
            headers["X-Service-Token"] = self.service_token
        url = f"{self.rag_url}/v1/knowledge-bases/{self.kb_id}/upload"
        try:
            resp = await client.post(
                url,
                files={"file": (filename, body.encode("utf-8"), "text/plain")},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        except Exception as e:
            logger.warning(
                f"⚠️ webcrawl upload failed for {page['url']}: {type(e).__name__}"
            )
            return None
        doc_id = data.get("document_id") if isinstance(data, dict) else None
        return str(doc_id) if doc_id else None

    # ── registry-driven reads (SAFE: preview only, never uploads) ──────────────
    async def collect_data(self) -> Dict[str, Any]:
        """Crawl + extract the configured URLs and record a **preview** — page
        count, titles, sizes. Deliberately does NOT upload, so the hourly loop
        can't silently re-ingest / duplicate KB documents. Use the ``ingest``
        action to actually write to the KB."""
        if not self.urls:
            self._last = {"message": "no seed URLs configured", "pages": []}
            return self._last
        pages = await self._crawl()
        self._last = {
            "pages": [
                {"url": p["url"], "title": p["title"], "chars": p["chars"]}
                for p in pages
            ],
            "page_count": len(pages),
            "kb_id": self.kb_id or None,
            "ingested": False,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            f"🕸️ webcrawl preview: {len(pages)} page(s) from {len(self.urls)} seed(s)"
        )
        return self._last

    async def analyze(self) -> Dict[str, Any]:
        """Return the most recent preview."""
        if not self._last:
            return {"message": "no data collected yet", "seed_urls": len(self.urls)}
        return self._last

    # ── actions (POST /v1/plugins/webcrawl/actions/<method>) ───────────────────
    async def refresh(self) -> Dict[str, Any]:
        """Re-run the read-only crawl+extract preview (same as the hourly loop)."""
        return await self.collect_data()

    async def ingest(self) -> Dict[str, Any]:
        """Crawl + extract + **upload** each page into the target KB (JWT-gated
        write). Returns per-page document ids. Requires WEBCRAWL_KB_ID."""
        if not self.kb_id:
            return {"error": "WEBCRAWL_KB_ID is not configured", "ingested": 0}
        if not self.urls:
            return {"error": "no seed URLs configured", "ingested": 0}
        pages = await self._crawl()
        documents: List[Dict[str, Any]] = []
        ingested = 0
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            for page in pages:
                doc_id = await self._upload(client, page)
                documents.append(
                    {
                        "url": page["url"],
                        "title": page["title"],
                        "chars": page["chars"],
                        "document_id": doc_id,
                    }
                )
                if doc_id:
                    ingested += 1
        result = {
            "kb_id": self.kb_id,
            "pages_crawled": len(pages),
            "ingested": ingested,
            "documents": documents,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            f"🕸️ webcrawl ingest: {ingested}/{len(pages)} page(s) → KB {self.kb_id}"
        )
        return result
