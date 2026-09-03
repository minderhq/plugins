"""Generate ``catalog.json`` — a machine-readable index of every plugin in this
catalog, built from the SDK's own introspection (the same data ``minder-plugin
inspect`` prints, plus ``register()`` metadata).

Run it:

    python scripts/gen_catalog.py            # writes catalog.json at the repo root
    python scripts/gen_catalog.py --check     # exit 1 if catalog.json is stale

The manifest is the decision-independent contract point for the marketplace's
catalog ingestion (minderhq/minder#1294 phase 2): whatever the eventual
vendoring mechanism, a consumer reads one JSON to discover names, versions,
capabilities, config schema, AI tools and display metadata — no need to import
plugin code. Regenerate it whenever you add or change a plugin (CONTRIBUTING).
"""

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from minder_plugin_sdk import (
    build_tool_definitions,
    capabilities,
    requirements,
    resolve_config_schema,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG_FILE = ROOT / "catalog.json"
_SKIP = {"tests", "scripts", "__pycache__"}


def _plugin_packages() -> List[str]:
    return sorted(
        p.name
        for p in ROOT.iterdir()
        if p.is_dir() and p.name not in _SKIP and (p / "__init__.py").exists()
    )


def _plugin_class(module: Any) -> Any:
    names = getattr(module, "__all__", None)
    if names:
        return getattr(module, names[0])
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ == module.__name__ and hasattr(obj, "register"):
            return obj
    raise RuntimeError(f"{module.__name__}: no plugin class with register()")


def _entry(pkg: str) -> Dict[str, Any]:
    module = importlib.import_module(pkg)
    plugin = _plugin_class(module)()
    md = asyncio.run(plugin.register())
    schema, ui = resolve_config_schema(plugin)
    return {
        "package": pkg,
        "name": getattr(md, "name", pkg),
        "version": getattr(md, "version", ""),
        "description": getattr(md, "description", ""),
        "author": getattr(md, "author", ""),
        "capabilities": sorted(capabilities(plugin)),
        "requires": requirements(plugin),
        "config_schema": schema,
        "ui_schema": ui,
        "ai_tools": build_tool_definitions(plugin),
        "display": getattr(type(plugin), "DISPLAY", {}),
    }


def build_catalog() -> Dict[str, Any]:
    """The full catalog manifest: one entry per plugin, sorted by name."""
    sys.path.insert(0, str(ROOT))
    entries = [_entry(pkg) for pkg in _plugin_packages()]
    entries.sort(key=lambda e: e["name"])
    return {"source": "minderhq/plugins", "count": len(entries), "plugins": entries}


def _serialize(catalog: Dict[str, Any]) -> str:
    return json.dumps(catalog, indent=2, sort_keys=False, default=str) + "\n"


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_catalog")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if catalog.json is out of date (don't write)",
    )
    args = parser.parse_args(argv)
    rendered = _serialize(build_catalog())
    if args.check:
        current = (
            CATALOG_FILE.read_text(encoding="utf-8") if CATALOG_FILE.exists() else ""
        )
        if current != rendered:
            print("catalog.json is stale — run `python scripts/gen_catalog.py`")
            return 1
        print("catalog.json is up to date")
        return 0
    CATALOG_FILE.write_text(rendered, encoding="utf-8")
    print(f"wrote {CATALOG_FILE.relative_to(ROOT)} ({rendered.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
