"""GitHub — public repository stats (community catalog plugin).

Polls the public, **keyless** GitHub REST API for the configured repositories'
star / fork / open-issue counts and sinks them to InfluxDB as a time series
(measurement ``github_repo``, tag ``repo``, fields ``stars``/``forks``/
``open_issues``) so you can chart a project's momentum in Grafana. Exposes a
``get_repo_stats`` AI tool the LLM can call ("how many stars does X have?").

Keyless by design — the anonymous REST API is enough for a handful of repos on
an hourly loop (GitHub's unauthenticated limit is 60 req/h per IP); a repo that
rate-limits or 404s is skipped, never crashing the collection loop. Uses only
``httpx`` (already in the plugin-registry image). A worked, self-contained
catalog plugin: config UI, an AI tool, a DISPLAY, and declared REQUIRES.

Config (all optional — keyless defaults):
  GH_REPOS          ``owner/repo`` entries, comma-separated (default: a few
                    well-known OSS repos; override per deployment).
  GH_SINK_INFLUXDB  "1"/"0" — write the star/fork/issue metric (default "1").
  GH_HTTP_TIMEOUT   per-request timeout seconds (default 10, env-only).
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from minder_plugin_sdk import PluginBase, PluginMetadata

__all__ = ["GitHubPlugin"]

logger = logging.getLogger("minder.plugin.github")

_API = "https://api.github.com"
# owner/repo — GitHub's own allowed charset (alnum, '-', '_', '.'); anything else
# is rejected before it's interpolated into the request path or a tag value.
_SAFE_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


class GitHubPlugin(PluginBase):
    DISPLAY = {
        "label": "GitHub Repos",
        "summary": "Track public repositories' stars/forks/issues over time, plus a stats tool.",
        "logo": "github",
        "color": "#24292f",
        "category": "data-source",
    }

    # Optional InfluxDB sink; works fine without it (stats still fetched/served).
    REQUIRES = {"services": [], "optional_services": ["influxdb"], "bundles": []}

    ACTIONS = frozenset({"refresh", "get_repo_stats"})
    READ_ONLY_ACTIONS = frozenset({"get_repo_stats"})

    CONFIG_SCHEMA = [
        {
            "key": "GH_REPOS",
            "type": "string",
            "default": "torvalds/linux,python/cpython,minderhq/plugin-sdk",
            "description": "Repositories to track, as 'owner/repo', comma-separated.",
            "widget": "textarea",
            "rows": 3,
            "group": "Repositories",
        },
        {
            "key": "GH_SINK_INFLUXDB",
            "type": "bool",
            "default": True,
            "description": "Write each collection to InfluxDB as a time series.",
            "widget": "toggle",
            "group": "Storage",
        },
    ]

    AI_TOOLS = [
        {
            "name": "get_repo_stats",
            "description": (
                "Get a public GitHub repository's current stars, forks and open "
                "issue count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository as 'owner/repo' (e.g. 'python/cpython').",
                    },
                },
                "required": ["repo"],
            },
            "action": "get_repo_stats",
            "method": "GET",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        self.http_timeout = float(os.environ.get("GH_HTTP_TIMEOUT", "10"))  # env-only
        super().__init__(config)

    def apply_config(self, cfg: Dict) -> None:
        if "GH_REPOS" in cfg:
            self.repos = self._parse_repos(str(cfg["GH_REPOS"] or ""))
        if "GH_SINK_INFLUXDB" in cfg:
            v = cfg["GH_SINK_INFLUXDB"]
            self.sink_influxdb = (
                v
                if isinstance(v, bool)
                else str(v).lower() in ("1", "true", "yes", "on")
            )

    @staticmethod
    def _parse_repos(spec: str) -> List[str]:
        """Comma-separated 'owner/repo' entries, lowercased+stripped, dropping any
        that don't match the safe owner/repo shape."""
        out: List[str] = []
        for item in spec.split(","):
            repo = item.strip()
            if repo and _SAFE_REPO.match(repo) and repo not in out:
                out.append(repo)
        return out

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def register(self) -> PluginMetadata:
        return PluginMetadata(
            name="github",
            version="1.0.0",
            description="Keyless GitHub repo stars/forks/issues time series + a stats tool.",
            author="minderhq",
            capabilities=["collect", "analyze", "github"],
            data_sources=["github"],
            databases=["influxdb"],
        )

    async def health_check(self) -> Dict:
        # MUST return {"healthy": <bool>} — the monitoring loop reads health["healthy"].
        return {
            "healthy": self.status == "ready",
            "repos": list(self.repos),
            "influxdb_sink": self.sink_influxdb,
        }

    # ── fetching ─────────────────────────────────────────────────────────────
    async def _fetch_stats(self, repo: str) -> Optional[Dict]:
        """Return {stars, forks, open_issues} for 'owner/repo', or None on error /
        an unsafe repo / a non-OK response. Never raises."""
        if not _SAFE_REPO.match(repo):
            logger.warning(f"⚠️ refusing GitHub fetch for unsafe repo: {repo!r}")
            return None
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout,
                headers={
                    "User-Agent": "minder-github-plugin",
                    "Accept": "application/vnd.github+json",
                },
            ) as client:
                resp = await client.get(f"{_API}/repos/{repo}")
                resp.raise_for_status()
                body = resp.json() or {}
        except Exception as e:
            logger.warning(f"⚠️ GitHub fetch failed for {repo}: {type(e).__name__}")
            return None
        return {
            "stars": body.get("stargazers_count"),
            "forks": body.get("forks_count"),
            "open_issues": body.get("open_issues_count"),
        }

    async def _write_influxdb(self, stats: Dict[str, Dict]) -> bool:
        """Write per-repo stats to InfluxDB (measurement 'github_repo')."""
        cfg = self.config.get("influxdb") or {}
        if not (self.sink_influxdb and cfg.get("enabled") and stats):
            return False
        host, port = cfg.get("host", "minder-influxdb"), cfg.get("port", 8086)
        org, bucket = cfg.get("org", "minder"), cfg.get("bucket", "minder-metrics")
        token = cfg.get("token", "")
        lines = []
        for repo, s in stats.items():
            fields = [
                f"{k}={int(v)}i"
                for k, v in (
                    ("stars", s.get("stars")),
                    ("forks", s.get("forks")),
                    ("open_issues", s.get("open_issues")),
                )
                if isinstance(v, int) and not isinstance(v, bool)
            ]
            if fields:
                # line-protocol tag values escape comma, equals AND space (a repo
                # is owner/repo — '/' is legal in a tag, but escape defensively).
                tag = repo.replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")
                lines.append(f"github_repo,repo={tag} {','.join(fields)}")
        if not lines:
            return False
        url = f"http://{host}:{port}/api/v2/write"
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                resp = await client.post(
                    url,
                    params={"org": org, "bucket": bucket, "precision": "s"},
                    headers={"Authorization": f"Token {token}"},
                    content="\n".join(lines),
                )
                resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"⚠️ InfluxDB write failed: {type(e).__name__}")
            return False

    # ── registry-driven reads ────────────────────────────────────────────────
    async def collect_data(self) -> Dict:
        """Fetch stats for all configured repos; store + write the metric."""
        stats: Dict[str, Dict] = {}
        for repo in self.repos:
            s = await self._fetch_stats(repo)
            if s is not None:
                stats[repo] = s
        wrote = await self._write_influxdb(stats)
        self._last = {
            "repos": stats,
            "influxdb_written": wrote,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"🐙 github collect: {len(stats)} repo(s), influx={wrote}")
        return self._last

    async def analyze(self) -> Dict:
        """Return the most recent collection."""
        if not self._last:
            return {"message": "no data collected yet", "repos": list(self.repos)}
        return self._last

    # ── actions ───────────────────────────────────────────────────────────────
    async def refresh(self) -> Dict:
        """Force an immediate re-collection (same as the hourly loop)."""
        return await self.collect_data()

    async def get_repo_stats(self, repo: str) -> Dict:
        """Current stars/forks/open-issues for a repo (backs the get_repo_stats tool)."""
        if not repo:
            return {"error": "repo is required"}
        repo = repo.strip()
        if not _SAFE_REPO.match(repo):
            return {"repo": repo, "error": "repo must be 'owner/repo'"}
        s = await self._fetch_stats(repo)
        if s is None:
            return {"repo": repo, "error": "stats unavailable"}
        return {"repo": repo, **s}
