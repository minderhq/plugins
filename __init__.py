"""Lets this whole repo double as the `plugins` package minderhq/minder's
plugin-registry needs.

This file has no other purpose and this repo is otherwise a flat layout
(each plugin is a top-level directory, not nested under a package -- see
pyproject.toml's `[tool.setuptools]`). When vendored as a git submodule at
minderhq/minder's `src/plugins/` (rather than a separate `src/plugins_catalog/`
alongside a near-empty `src/plugins/`, #1460's original approach), the
plugin-registry's Dockerfile just `COPY`s this whole checkout to `/app/plugins`
and `plugin_loader.py` imports each plugin as `plugins.<name>` -- which only
resolves if `plugins` itself is a real package. Nothing here is imported by
this repo's own tests or tooling (`pythonpath = ["."]` already puts the repo
root on `sys.path`, so `import crypto` etc. work regardless of this file).
"""
