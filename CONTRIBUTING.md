# Contributing a plugin

1. Scaffold from [minderhq/plugin-template](https://github.com/minderhq/plugin-template)
   or `minder-plugin scaffold <name>`.
2. Add it as `<name>/__init__.py`, importing from `minder_plugin_sdk`. Declare a
   `register()` returning `PluginMetadata`, and any of `CONFIG_SCHEMA` / `ACTIONS`
   / `AI_TOOLS` / `DISPLAY` / `REQUIRES` you need.
3. Validate locally:
   ```bash
   pip install -e ".[dev]"
   minder-plugin validate <name>/__init__.py
   pytest -q
   ```
4. Regenerate the machine-readable catalog index and add a row to the README
   catalog table, then open a PR. CI runs `minder-plugin validate` on every
   plugin and `pytest` (auto-discovers each; a test fails if `catalog.json` is
   stale).
   ```bash
   python scripts/gen_catalog.py    # refresh catalog.json
   ```

Design & contract: https://github.com/minderhq/plugin-sdk
Governance: https://github.com/minderhq/minder/blob/main/docs/development/issue-and-pr-conventions.md
