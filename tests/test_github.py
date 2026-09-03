"""Behavioural tests for the github plugin — repo-spec parsing, config coercion,
the stats fetch/parse (incl. the unsafe-repo guard + fail-soft None), the
get_repo_stats action, collect_data aggregation, and the InfluxDB tag escaping.
HTTP is faked; no network.
"""

import asyncio

import pytest

import github
from github import GitHubPlugin


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
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(payload)

        async def post(self, url, params=None, headers=None, content=None):
            if capture is not None:
                capture["content"] = content
            return _FakeResp({})

    return _Client


# ── _parse_repos ─────────────────────────────────────────────────────────────
def test_parse_repos_keeps_valid_owner_repo():
    assert GitHubPlugin._parse_repos(" a/b , c/d ") == ["a/b", "c/d"]


def test_parse_repos_drops_malformed_and_dedupes():
    # no slash, unsafe char ('!'), and a duplicate 'a/b' — all dropped
    assert GitHubPlugin._parse_repos("noslash, a/b!, a/b, ok/repo, a/b") == [
        "a/b",
        "ok/repo",
    ]


def test_parse_repos_dedup_preserves_first_seen_order():
    assert GitHubPlugin._parse_repos("a/b, ok/repo, a/b") == ["a/b", "ok/repo"]


def test_parse_repos_empty():
    assert GitHubPlugin._parse_repos("") == []
    assert GitHubPlugin._parse_repos("  ,  ") == []


# ── apply_config ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("1", True), ("yes", True), ("0", False), ("no", False)],
)
def test_apply_config_sink_bool(value, expected):
    p = GitHubPlugin()
    p.apply_config({"GH_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


# ── _fetch_stats ─────────────────────────────────────────────────────────────
def test_fetch_stats_maps_api_fields(monkeypatch):
    p = GitHubPlugin()
    payload = {"stargazers_count": 100, "forks_count": 20, "open_issues_count": 5}
    monkeypatch.setattr(github.httpx, "AsyncClient", _client(payload))
    assert asyncio.run(p._fetch_stats("a/b")) == {
        "stars": 100,
        "forks": 20,
        "open_issues": 5,
    }


def test_fetch_stats_rejects_unsafe_repo(monkeypatch):
    p = GitHubPlugin()
    monkeypatch.setattr(github.httpx, "AsyncClient", _client({}))
    assert asyncio.run(p._fetch_stats("not-a-repo")) is None
    assert asyncio.run(p._fetch_stats("a/b?x=1")) is None


def test_fetch_stats_none_on_error(monkeypatch):
    p = GitHubPlugin()
    monkeypatch.setattr(github.httpx, "AsyncClient", _client({}, raise_on_get=True))
    assert asyncio.run(p._fetch_stats("a/b")) is None


# ── get_repo_stats ───────────────────────────────────────────────────────────
def test_get_repo_stats_requires_repo():
    assert asyncio.run(GitHubPlugin().get_repo_stats("")) == {
        "error": "repo is required"
    }


def test_get_repo_stats_rejects_bad_shape(monkeypatch):
    p = GitHubPlugin()
    res = asyncio.run(p.get_repo_stats("justname"))
    assert res == {"repo": "justname", "error": "repo must be 'owner/repo'"}


def test_get_repo_stats_unavailable(monkeypatch):
    p = GitHubPlugin()
    monkeypatch.setattr(github.httpx, "AsyncClient", _client({}, raise_on_get=True))
    assert asyncio.run(p.get_repo_stats("a/b")) == {
        "repo": "a/b",
        "error": "stats unavailable",
    }


def test_get_repo_stats_success(monkeypatch):
    p = GitHubPlugin()
    payload = {"stargazers_count": 9, "forks_count": 3, "open_issues_count": 1}
    monkeypatch.setattr(github.httpx, "AsyncClient", _client(payload))
    assert asyncio.run(p.get_repo_stats(" a/b ")) == {
        "repo": "a/b",
        "stars": 9,
        "forks": 3,
        "open_issues": 1,
    }


# ── collect_data + influx escaping ───────────────────────────────────────────
def test_collect_data_skips_failed_and_gates_influx(monkeypatch):
    p = GitHubPlugin()
    p.repos = ["a/b", "c/d"]
    p.sink_influxdb = False

    async def _stats(repo):
        return None if repo == "c/d" else {"stars": 1, "forks": 2, "open_issues": 3}

    monkeypatch.setattr(p, "_fetch_stats", _stats)
    res = asyncio.run(p.collect_data())
    assert set(res["repos"]) == {"a/b"}
    assert res["influxdb_written"] is False


def test_write_influxdb_emits_escaped_line_protocol(monkeypatch):
    p = GitHubPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    cap = {}
    monkeypatch.setattr(github.httpx, "AsyncClient", _client({}, capture=cap))
    ok = asyncio.run(
        p._write_influxdb({"owner/repo": {"stars": 5, "forks": 2, "open_issues": 1}})
    )
    assert ok is True
    # '/' is legal unescaped in a tag; fields are integers with an 'i' suffix
    assert (
        cap["content"] == "github_repo,repo=owner/repo stars=5i,forks=2i,open_issues=1i"
    )


def test_write_influxdb_skips_non_int_fields(monkeypatch):
    p = GitHubPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    cap = {}
    monkeypatch.setattr(github.httpx, "AsyncClient", _client({}, capture=cap))
    # a repo whose API returned None counts contributes no fields → no line → False
    ok = asyncio.run(
        p._write_influxdb({"a/b": {"stars": None, "forks": None, "open_issues": None}})
    )
    assert ok is False
