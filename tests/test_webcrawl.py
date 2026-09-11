"""Behavioural tests for the webcrawl connector plugin — beyond the shared
contract/lifecycle checks in test_catalog.py, these exercise the real logic:
URL/config parsing + coercion, the SSRF gate (incl. redirects), HTML text
extraction, bounded same-domain crawl, the read-only preview vs. the JWT-gated
KB-ingest write, and the upload handoff. All HTTP is faked; no network.
"""

import asyncio
from types import SimpleNamespace

import pytest

import webcrawl
from webcrawl import WebCrawlPlugin, _HTMLTextExtractor

HTML = """<html><head><title>Doc Title</title></head><body>
  <h1>Heading</h1>
  <p>Hello world.</p>
  <script>var secret = 1;</script>
  <style>.x{color:red}</style>
  <a href="/page2">next</a>
  <a href="https://other.example/x">offsite</a>
  <a href="mailto:a@b.c">mail</a>
</body></html>"""


class _FakeResp:
    def __init__(self, text="", *, headers=None, json_data=None, status_ok=True):
        self.text = text
        self.content = text.encode() if text else b"{}"
        self.headers = headers or {"content-type": "text/html"}
        self._json = json_data if json_data is not None else {}
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")

    def json(self):
        return self._json


class _FakeClient:
    """httpx.AsyncClient stand-in mapping url→_FakeResp for get, and recording posts."""

    def __init__(self, get_map=None, post_resp=None, post_exc=None, **_):
        self._get_map = get_map or {}
        self._post_resp = post_resp
        self._post_exc = post_exc
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        resp = self._get_map.get(url)
        if resp is None:
            raise RuntimeError("unexpected url")
        return resp

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self._post_exc:
            raise self._post_exc
        return self._post_resp or _FakeResp(json_data={"document_id": "doc-1"})


def _plugin(monkeypatch, *, safe=True):
    """A WebCrawlPlugin with the SSRF DNS check stubbed and rate-limit off."""

    async def _check(url):
        return safe

    monkeypatch.setattr(webcrawl, "_is_safe_url", _check)
    p = WebCrawlPlugin()
    p.rate_limit = 0  # no politeness sleep in tests
    return p


# ── config parsing / coercion ────────────────────────────────────────────────
def test_parse_urls_splits_and_dedupes():
    out = WebCrawlPlugin._parse_urls(
        "https://a.example, https://b.example\nhttps://a.example"
    )
    assert out == ["https://a.example", "https://b.example"]


def test_parse_urls_empty():
    assert WebCrawlPlugin._parse_urls("") == []
    assert WebCrawlPlugin._parse_urls("  ,  \n ") == []


def test_coerce_int_clamps_and_falls_back():
    assert WebCrawlPlugin._coerce_int("5", 0, lo=0, hi=10) == 5
    assert WebCrawlPlugin._coerce_int("99", 0, lo=0, hi=10) == 10  # clamp hi
    assert WebCrawlPlugin._coerce_int("-1", 0, lo=0, hi=10) == 0  # clamp lo
    assert WebCrawlPlugin._coerce_int("nope", 7, lo=0, hi=10) == 7  # fallback


def test_apply_config_maps_all_keys(monkeypatch):
    p = _plugin(monkeypatch)
    p.apply_config(
        {
            "WEBCRAWL_KB_ID": " kb-9 ",
            "WEBCRAWL_URLS": "https://x.example",
            "WEBCRAWL_MAX_DEPTH": "2",
            "WEBCRAWL_MAX_PAGES": "5",
            "WEBCRAWL_RATE_LIMIT": "0.0",
            "WEBCRAWL_RAG_URL": "http://rag:8004/",
            "WEBCRAWL_SERVICE_TOKEN": "tok",
        }
    )
    assert p.kb_id == "kb-9"
    assert p.urls == ["https://x.example"]
    assert p.max_depth == 2
    assert p.max_pages == 5
    assert p.rag_url == "http://rag:8004"  # trailing slash stripped
    assert p.service_token == "tok"


def test_defaults_are_safe():
    p = WebCrawlPlugin()
    assert p.urls == []
    assert p.kb_id == ""
    assert p.max_depth == 0
    assert p.max_pages == 20


# ── lifecycle ─────────────────────────────────────────────────────────────────
def test_register_and_health():
    p = WebCrawlPlugin()
    md = asyncio.run(p.register())
    assert md.name == "webcrawl"
    assert "connector" in md.capabilities
    h = asyncio.run(p.health_check())
    assert h["healthy"] is True


def test_lifecycle_status_transitions():
    p = WebCrawlPlugin()
    assert p.status == "registered"
    asyncio.run(p.initialize())
    assert p.status == "ready"
    asyncio.run(p.shutdown())
    assert p.status == "shutdown"


# ── HTML extraction ───────────────────────────────────────────────────────────
def test_html_extractor_gets_title_text_and_links_drops_scripts():
    ex = _HTMLTextExtractor()
    ex.feed(HTML)
    assert ex.title == "Doc Title"
    text = ex.text()
    assert "Hello world." in text
    assert "Heading" in text
    assert "secret" not in text  # <script> dropped
    assert "color:red" not in text  # <style> dropped
    assert "/page2" in ex.links
    assert "https://other.example/x" in ex.links


# ── SSRF gate: the real classifier ────────────────────────────────────────────
def _resolving_to(monkeypatch, ips):
    class _FakeLoop:
        async def getaddrinfo(self, host, port):
            if ips is OSError:
                raise OSError("resolution failed")
            return [(2, 1, 6, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(webcrawl.asyncio, "get_running_loop", lambda: _FakeLoop())


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",  # scheme not in http(s) allowlist
        "https://",  # no hostname
        "file:///etc/passwd",  # non-web scheme
    ],
)
def test_is_safe_url_rejects_bad_scheme_or_hostless(monkeypatch, url):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    assert asyncio.run(webcrawl._is_safe_url(url)) is False


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local cloud-metadata endpoint
        "240.0.0.1",  # reserved
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "::1",  # ipv6 loopback
    ],
)
def test_is_safe_url_rejects_internal_addresses(monkeypatch, ip):
    _resolving_to(monkeypatch, [ip])
    assert asyncio.run(webcrawl._is_safe_url("https://target.example/x")) is False


def test_is_safe_url_allows_public_http_and_https(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    assert asyncio.run(webcrawl._is_safe_url("https://example.com/x")) is True
    assert asyncio.run(webcrawl._is_safe_url("http://example.com/x")) is True


def test_is_safe_url_rejects_dual_answer_with_internal(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34", "10.1.2.3"])
    assert asyncio.run(webcrawl._is_safe_url("https://dual.example/x")) is False


def test_is_safe_url_fails_closed_on_resolution_error(monkeypatch):
    _resolving_to(monkeypatch, OSError)
    assert asyncio.run(webcrawl._is_safe_url("https://nx.example/x")) is False


# ── redirect re-vetting hook ───────────────────────────────────────────────────
def _redirect_resp(*, is_redirect, location, request_url="https://a.example/x"):
    return SimpleNamespace(
        is_redirect=is_redirect,
        headers=({"location": location} if location is not None else {}),
        request=SimpleNamespace(url=webcrawl.httpx.URL(request_url)),
    )


def test_reject_unsafe_redirect_raises_on_unsafe_target(monkeypatch):
    async def _unsafe(url):
        return False

    monkeypatch.setattr(webcrawl, "_is_safe_url", _unsafe)
    resp = _redirect_resp(
        is_redirect=True, location="https://169.254.169.254/latest/meta-data/"
    )
    with pytest.raises(webcrawl.httpx.HTTPError):
        asyncio.run(webcrawl._reject_unsafe_redirect(resp))


def test_reject_unsafe_redirect_allows_safe_and_ignores_non_redirect(monkeypatch):
    async def _safe(url):
        return True

    monkeypatch.setattr(webcrawl, "_is_safe_url", _safe)
    asyncio.run(
        webcrawl._reject_unsafe_redirect(
            _redirect_resp(is_redirect=True, location="https://cdn.example/x")
        )
    )
    asyncio.run(
        webcrawl._reject_unsafe_redirect(
            _redirect_resp(is_redirect=False, location=None)
        )
    )


# ── _fetch: SSRF gate + content-type + byte cap ───────────────────────────────
def test_fetch_rejects_unsafe_url(monkeypatch):
    p = _plugin(monkeypatch, safe=False)
    client = _FakeClient(get_map={"https://internal": _FakeResp(HTML)})
    assert asyncio.run(p._fetch(client, "https://internal")) is None


def test_fetch_skips_non_text_content(monkeypatch):
    p = _plugin(monkeypatch)
    resp = _FakeResp("binary", headers={"content-type": "image/png"})
    client = _FakeClient(get_map={"https://a/x": resp})
    assert asyncio.run(p._fetch(client, "https://a/x")) is None


def test_fetch_skips_oversized_by_content_length(monkeypatch):
    p = _plugin(monkeypatch)
    p.max_bytes = 10
    resp = _FakeResp(
        "hello", headers={"content-type": "text/html", "content-length": "999"}
    )
    client = _FakeClient(get_map={"https://a/x": resp})
    assert asyncio.run(p._fetch(client, "https://a/x")) is None


def test_fetch_returns_html(monkeypatch):
    p = _plugin(monkeypatch)
    client = _FakeClient(get_map={"https://a/x": _FakeResp(HTML)})
    assert asyncio.run(p._fetch(client, "https://a/x")) == HTML


# ── same-host link filtering ──────────────────────────────────────────────────
def test_same_host_links_filters_offsite_and_non_http(monkeypatch):
    p = _plugin(monkeypatch)
    links = [
        "/page2",
        "https://other.example/x",
        "mailto:a@b.c",
        "#frag",
        "https://a.example/y#z",
    ]
    out = p._same_host_links("https://a.example/start", links)
    assert out == ["https://a.example/page2", "https://a.example/y"]


# ── bounded crawl ─────────────────────────────────────────────────────────────
def test_crawl_respects_max_pages(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.max_depth = 5
    p.max_pages = 2
    page = "<html><body><a href='/2'>2</a><a href='/3'>3</a>x</body></html>"
    resp = _FakeResp(page)
    monkeypatch.setattr(
        webcrawl.httpx,
        "AsyncClient",
        lambda **k: _FakeClient(
            get_map={
                "https://a.example/1": resp,
                "https://a.example/2": resp,
                "https://a.example/3": resp,
            }
        ),
    )
    pages = asyncio.run(p._crawl())
    assert len(pages) == 2  # capped despite more links available


def test_crawl_depth_zero_only_seeds(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.max_depth = 0
    resp = _FakeResp("<html><body><a href='/2'>2</a>hi</body></html>")
    monkeypatch.setattr(
        webcrawl.httpx,
        "AsyncClient",
        lambda **k: _FakeClient(get_map={"https://a.example/1": resp}),
    )
    pages = asyncio.run(p._crawl())
    assert [pg["url"] for pg in pages] == ["https://a.example/1"]


# ── collect_data / analyze: read-only preview (never uploads) ─────────────────
def test_collect_data_no_urls_is_noop():
    p = WebCrawlPlugin()
    res = asyncio.run(p.collect_data())
    assert res["pages"] == []
    assert "no seed URLs" in res["message"]


def test_collect_data_is_preview_not_ingest(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.kb_id = "kb-1"
    monkeypatch.setattr(
        p,
        "_crawl",
        _async_return(
            [{"url": "https://a.example/1", "title": "T", "text": "body", "chars": 4}]
        ),
    )
    res = asyncio.run(p.collect_data())
    assert res["ingested"] is False
    assert res["page_count"] == 1
    assert res["pages"][0] == {"url": "https://a.example/1", "title": "T", "chars": 4}


def test_analyze_before_and_after(monkeypatch):
    p = WebCrawlPlugin()
    assert "no data collected yet" in asyncio.run(p.analyze())["message"]


# ── ingest action: the JWT-gated write / upload handoff ───────────────────────
def test_ingest_requires_kb_id(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.kb_id = ""
    res = asyncio.run(p.ingest())
    assert res["ingested"] == 0
    assert "WEBCRAWL_KB_ID" in res["error"]


def test_ingest_uploads_each_page_and_reports_doc_ids(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.kb_id = "kb-7"
    p.service_token = "svc"
    monkeypatch.setattr(
        p,
        "_crawl",
        _async_return(
            [{"url": "https://a.example/1", "title": "T", "text": "body", "chars": 4}]
        ),
    )
    fake = _FakeClient(post_resp=_FakeResp(json_data={"document_id": "doc-42"}))
    monkeypatch.setattr(webcrawl.httpx, "AsyncClient", lambda **k: fake)
    res = asyncio.run(p.ingest())
    assert res["ingested"] == 1
    assert res["documents"][0]["document_id"] == "doc-42"
    # verify the handoff hit the real upload endpoint with the service token
    url, kwargs = fake.posts[0]
    assert url == "http://minder-rag-pipeline:8004/v1/knowledge-bases/kb-7/upload"
    assert "file" in kwargs["files"]
    assert kwargs["headers"]["X-Service-Token"] == "svc"


def test_ingest_counts_only_successful_uploads(monkeypatch):
    p = _plugin(monkeypatch)
    p.urls = ["https://a.example/1"]
    p.kb_id = "kb-7"
    monkeypatch.setattr(
        p,
        "_crawl",
        _async_return(
            [{"url": "https://a.example/1", "title": "T", "text": "b", "chars": 1}]
        ),
    )
    fake = _FakeClient(post_exc=RuntimeError("upload boom"))
    monkeypatch.setattr(webcrawl.httpx, "AsyncClient", lambda **k: fake)
    res = asyncio.run(p.ingest())
    assert res["ingested"] == 0
    assert res["documents"][0]["document_id"] is None


def test_refresh_calls_collect_data(monkeypatch):
    p = _plugin(monkeypatch)
    monkeypatch.setattr(p, "collect_data", _async_return({"ok": True}))
    assert asyncio.run(p.refresh()) == {"ok": True}


def _async_return(value):
    async def _f(*a, **k):
        return value

    return _f
