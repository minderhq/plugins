# Minder plugins

The **catalog** of [Minder](https://github.com/minderhq/minder) plugins —
first-party and community — each validated against the
[plugin-sdk](https://github.com/minderhq/plugin-sdk).

Every plugin lives in its own top-level directory as a package:

```
<name>/__init__.py     # the plugin class (imports from minder_plugin_sdk)
```

## How it reaches a running Minder

This repo is **vendored into the core** at `src/plugins/` via a git submodule, so
the plugin-registry loads the whole catalog on startup — no per-plugin install, and
a `--recurse-submodules` clone stays offline-friendly (see
[minderhq/minder#1256](https://github.com/minderhq/minder/issues/1256)).

> **Status:** the submodule vendoring is being wired up; until it lands, the
> shipped first-party plugins still live in the core repo. New catalog plugins are
> developed and validated here.

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
| [`frankfurter`](frankfurter) | Keyless ECB foreign-exchange rates + a `convert` AI tool. |
| [`hackernews`](hackernews) | Keyless Hacker News top stories + a `top_stories` AI tool. |

## License

Apache-2.0 (see [LICENSE](LICENSE)). Individual plugins may declare their own.
