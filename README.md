# Minder plugins

The **catalog** of [Minder](https://github.com/minderhq/minder) plugins —
first-party and community — each validated against the
[plugin-sdk](https://github.com/minderhq/plugin-sdk).

Every plugin lives in its own top-level directory as a package:

```
<name>/__init__.py     # the plugin class (imports from minder_plugin_sdk)
```

## How it reaches a running Minder

The plugin-registry **discovers and loads module plugins on startup** and lists
them at `/v1/plugins`. By design nothing runs arbitrary code — a plugin is fixed
handlers (or a declarative manifest), never uploaded code.

> **Status:** wiring this public catalog into a running core (git-submodule
> vendoring at `src/plugins/`, the way the web client already is) is planned but
> **not yet wired** — tracked in
> [minderhq/minder#1303](https://github.com/minderhq/minder/issues/1303). Today
> the first-party plugins shipped inside the core still live in the core repo;
> new catalog plugins are developed and validated here.

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
| [`crypto`](crypto) | Keyless daily crypto close prices (Yahoo) → InfluxDB + a `get_price` AI tool. |
| [`frankfurter`](frankfurter) | Keyless ECB foreign-exchange rates + a `convert` AI tool. |
| [`github`](github) | Keyless public-repo stars/forks/issues time series + a `get_repo_stats` AI tool. |
| [`hackernews`](hackernews) | Keyless Hacker News top stories + a `top_stories` AI tool. |
| [`news`](news) | Keyless RSS/Atom headlines + per-feed volume metric + a `get_news` AI tool. |
| [`portfolio`](portfolio) | Per-user holdings/watchlist price tracking (Yahoo) → InfluxDB + a `get_value` action. |
| [`tefas_funds`](tefas_funds) | Keyless TEFAS (Turkish fund) daily prices → InfluxDB + a `get_fund_price` AI tool. |
| [`weather`](weather) | Keyless Open-Meteo current-conditions time series + a `get_weather` AI tool. |
| [`wikipedia`](wikipedia) | Keyless Wikipedia article-summary lookup, exposed as a `wiki_summary` AI tool (a "Talent"). |

## License

Apache-2.0 (see [LICENSE](LICENSE)). Individual plugins may declare their own.
