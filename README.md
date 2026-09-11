# Minder plugins

The **catalog** of [Minder](https://github.com/minderhq/minder) plugins —
first-party and community — each validated against the
[plugin-sdk](https://github.com/minderhq/plugin-sdk).

See the [plugin docs](https://minderhq.github.io/docs/plugins/) — authoring, the
contract, and [publishing to this catalog](https://minderhq.github.io/docs/plugins/publishing/).

Every plugin lives in its own top-level directory as a package:

```
<name>/__init__.py     # the plugin class (imports from minder_plugin_sdk)
```

## How it reaches a running Minder

The plugin-registry **discovers and loads module plugins on startup** and lists
them at `/v1/plugins`. By design nothing runs arbitrary code — a plugin is fixed
handlers (or a declarative manifest), never uploaded code.

> **Status:** wired. This catalog is vendored into `minderhq/minder` as a git
> submodule (`src/plugins_catalog/`); its Dockerfile merges every plugin here
> into the running plugin-registry at build time (minderhq/minder#1460/#1472).
> `network`/`telegraf` (Minder's own first-party infra plugins) moved here
> too, so this repo is now the single source for every plugin — first-party
> and community alike, no plugin duplicated between the two repos.

## Contributing a plugin

1. Start from [**minderhq/plugin-template**](https://github.com/minderhq/plugin-template)
   (“Use this template”) or `minder-plugin scaffold <name>`.
2. Drop it in as `<name>/__init__.py`, importing from `minder_plugin_sdk`.
3. Make it pass — CI runs `minder-plugin validate` on every plugin plus
   `pytest`, which auto-discovers and contract-checks each one:

   ```bash
   pip install -e ".[dev]"
   minder-plugin validate frankfurter/__init__.py
   pytest -q
   ```
4. Open a PR. See [CONTRIBUTING](CONTRIBUTING.md).

## In the catalog

| Plugin | What |
|--------|------|
| [`arxiv`](arxiv) | Keyless arXiv paper search + a `search_papers` AI tool. |
| [`crypto`](crypto) | Keyless daily crypto close prices (Yahoo) → InfluxDB + a `get_price` AI tool. |
| [`frankfurter`](frankfurter) | Keyless ECB foreign-exchange rates + a `convert` AI tool. |
| [`github`](github) | Keyless public-repo stars/forks/issues time series + a `get_repo_stats` AI tool. |
| [`hackernews`](hackernews) | Keyless Hacker News top stories + a `top_stories` AI tool. |
| [`network`](network) | Autonomous nmap + SNMP host/service discovery, fanned out to telegraf/PostgreSQL/Neo4j/RabbitMQ. Minder's own first-party infra plugin. |
| [`news`](news) | Keyless RSS/Atom headlines + per-feed volume metric + a `get_news` AI tool. |
| [`openlibrary`](openlibrary) | Keyless Open Library book lookup + a `search_books` AI tool. |
| [`portfolio`](portfolio) | Per-user holdings/watchlist price tracking (Yahoo) → InfluxDB + a `get_value` action. |
| [`tefas_funds`](tefas_funds) | Keyless TEFAS (Turkish fund) daily prices → InfluxDB + a `get_fund_price` AI tool. |
| [`telegraf`](telegraf) | Manages telegraf's config "managed region" + reloads it. Minder's own first-party infra plugin. |
| [`weather`](weather) | Keyless Open-Meteo current-conditions time series + a `get_weather` AI tool. |
| [`webcrawl`](webcrawl) | Connector: crawls a configured list of public URLs (optional shallow same-domain depth), extracts readable text, and ingests it into a knowledge base via the rag upload API. SSRF-guarded + bounded crawl. |
| [`wikipedia`](wikipedia) | Keyless Wikipedia article-summary lookup, exposed as a `wiki_summary` AI tool (a "Talent"). |

## Running inside a live Minder instance

This is how the SDK-agnostic parts of the running platform treat a loaded
plugin — worth knowing when writing one, even though it's Minder's
behavior, not the SDK's:

- **`health_check()` must return `{"healthy": <bool>}`.** The monitoring
  loop reads `health.get("healthy")`; any other key (e.g. `"status"`) is
  treated as unhealthy.
- **Actions are JWT-gated, reads are not.** `GET /v1/plugins/<name>/collect`
  (→ `collect_data`, also runs hourly unauthenticated inside the service)
  and `GET /v1/plugins/<name>/analysis` (→ `analyze`) are open reads.
  `POST /v1/plugins/<name>/actions/<method>` requires a Bearer JWT and only
  reaches methods named in the plugin's `ACTIONS` frozenset — nothing else
  on the instance is callable this way.
- **Keep runtime deps minimal and declared.** Whatever a plugin imports (at
  module scope or lazily) becomes a real dependency of the running
  plugin-registry image — declare it in this repo's own `pyproject.toml`
  dev deps too if a test needs to exercise that code path (as `network`'s
  `asyncpg`-based sink test does).
- **Manifest vs. module is exclusive per plugin.** A `manifest.{json,yml,
  yaml}` takes precedence over `__init__.py` if both exist — a module
  plugin can't also ship a manifest. Module plugins don't need one to
  appear in `GET /v1/plugins/ai/tools`, though: the loader reads the
  instance's `AI_TOOLS` attribute directly if present.

## License

Apache-2.0 (see [LICENSE](LICENSE)). Individual plugins may declare their own.
