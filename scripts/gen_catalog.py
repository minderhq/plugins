"""Generate ``catalog.json`` — a machine-readable index of every plugin in this
catalog, built from the SDK's own introspection (the same data ``minder-plugin
inspect`` prints, plus ``register()`` metadata).

Run it:

    python scripts/gen_catalog.py            # writes catalog.json at the repo root
    python scripts/gen_catalog.py --check     # exit 1 if catalog.json is stale

The manifest is the decision-independent contract point for the marketplace's
catalog ingestion: whatever the eventual
vendoring mechanism, a consumer reads one JSON to discover names, versions,
capabilities, config schema, AI tools and display metadata — no need to import
plugin code. Regenerate it whenever you add or change a plugin (CONTRIBUTING).
"""

import argparse
import asyncio
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from minder_plugin_sdk import (
    build_tool_definitions,
    capabilities,
    contract_version,
    requirements,
    resolve_config_schema,
)

ROOT = Path(__file__).resolve().parent.parent

# Execution tiers, cheapest first, and the cheapest tier each runtime can run
# in: T0 declarative engine, T2 a worker in a shared runner group, T3 a
# container, T4 the author's own infrastructure. A code plugin in this
# catalog is a Python package, so its runtime is "python".
TIERS = ("T0", "T1", "T2", "T3", "T4")
RUNTIME_MIN_TIER = {
    "declarative": "T0",
    "python": "T2",
    "image": "T3",
    "remote": "T4",
}


def compute_tier(runtime: str, floor: Any = None) -> str:
    """tier = max(need(runtime), floor) -- the ADR's
    ``max(need(manifest), trust_floor, operator_override)`` with the two
    floors folded into one argument (max is associative). ``need`` is driven
    by the runtime only for now; no trust floor or override applies yet."""
    need = RUNTIME_MIN_TIER[runtime]
    if floor is None:
        return need
    if floor not in TIERS:
        raise ValueError(f"unknown tier floor: {floor!r}")
    return max(need, floor, key=TIERS.index)


CATALOG_FILE = ROOT / "catalog.json"
_SKIP = {"tests", "scripts", "__pycache__"}

# The owner-approved trust list (ADR: out-of-process plugins, increment 4,
# D1.6): which runner group each plugin is assigned to.
RUNNER_GROUPS_FILE = ROOT / "trust" / "runner-groups.yml"
_RUNNER_GROUP_KEYS = ("trusted", "privileged")
# A plugin name: the register() name the catalog is keyed by. Kept narrow so a
# malformed line can't smuggle arbitrary text past the fail-closed parser.
_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_LIST_ITEM_RE = re.compile(r"\s+-\s+(.+)")


class RunnerGroupError(Exception):
    """A violation of the runner-group trust contract (D1.6): a malformed
    ``trust/runner-groups.yml``, a plugin missing from it, or a plugin that
    needs ``privileged`` but isn't listed there."""


def _load_runner_groups(path: Path = RUNNER_GROUPS_FILE) -> Dict[str, str]:
    """Parse ``trust/runner-groups.yml`` into ``{plugin_name: group}`` WITHOUT
    pyyaml (it isn't installed in this catalog). A minimal, fail-closed parser
    for exactly this file's shape: two top-level keys (``trusted:`` /
    ``privileged:``), each followed by ``  - name`` lines; blank and ``#``
    lines are ignored; anything else is rejected."""
    if not path.exists():
        raise RunnerGroupError(f"{path.name} is missing — it is required (D1.6)")
    mapping: Dict[str, str] = {}
    seen_keys: set[str] = set()
    current: str | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            # A top-level key: must be exactly ``trusted:`` or ``privileged:``.
            key = line[:-1] if line.endswith(":") else None
            if key not in _RUNNER_GROUP_KEYS:
                raise RunnerGroupError(
                    f"{path.name}:{lineno}: expected 'trusted:' or 'privileged:', "
                    f"got {raw!r}"
                )
            if key in seen_keys:
                raise RunnerGroupError(f"{path.name}:{lineno}: duplicate '{key}:'")
            seen_keys.add(key)
            current = key
            continue
        # An indented line: must be a ``- name`` list item under a group.
        if current is None:
            raise RunnerGroupError(
                f"{path.name}:{lineno}: list item before any 'trusted:'/'privileged:'"
                f" key: {raw!r}"
            )
        item = _LIST_ITEM_RE.fullmatch(line)
        name = item.group(1).strip() if item else None
        if not name or not _PLUGIN_NAME_RE.fullmatch(name):
            raise RunnerGroupError(
                f"{path.name}:{lineno}: expected '  - <plugin-name>', got {raw!r}"
            )
        if name in mapping:
            raise RunnerGroupError(
                f"{path.name}:{lineno}: '{name}' is listed more than once "
                f"(it must appear in exactly one group)"
            )
        mapping[name] = current
    return mapping


def _resolve_runner_groups(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Set each entry's ``runner_group`` from the trust list and enforce D1.6.

    Exits non-zero (via :class:`RunnerGroupError`) when any plugin is unlisted,
    listed under an unknown name, or needs ``privileged`` (declares ``manages``
    or ``egress == "lan"``) without being listed there. Runs in both write and
    ``--check`` mode, since ``build_catalog`` is called in both."""
    groups = _load_runner_groups()
    names = {e["name"] for e in entries}
    errors: List[str] = []

    # A name in the trust list with no matching plugin is a typo or stale entry.
    for listed in sorted(set(groups) - names):
        errors.append(
            f"'{listed}' is in trust/runner-groups.yml but is not a plugin in this "
            f"catalog (remove it, or fix the name)"
        )

    resolved: List[Dict[str, Any]] = []
    for entry in entries:
        name = entry["name"]
        group = groups.get(name)
        if group is None:
            errors.append(f"add {name} to trust/runner-groups.yml")
        else:
            needs_privileged = (
                bool(entry.get("manages")) or entry.get("egress") == "lan"
            )
            if needs_privileged and group != "privileged":
                reason = (
                    "declares MANAGES" if entry.get("manages") else 'has EGRESS = "lan"'
                )
                errors.append(
                    f"{name} {reason} and must be in 'privileged', not {group!r} "
                    f"(trust/runner-groups.yml)"
                )
        # Insert runner_group right after "tier", preserving key order.
        rebuilt: Dict[str, Any] = {}
        for key, value in entry.items():
            rebuilt[key] = value
            if key == "tier":
                rebuilt["runner_group"] = group
        resolved.append(rebuilt)

    if errors:
        raise RunnerGroupError(
            "runner-group trust check failed (ADR D1.6):\n  - " + "\n  - ".join(errors)
        )
    return resolved


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


def _declarations(cls: Any, plugin: Any, md: Any) -> Dict[str, Any]:
    """The contract-v2 declarations a host derives grants and placement from
    (plugin-sdk ``declarations``): ``egress`` (``"public"`` / ``"lan"`` / a host
    list, ``None`` when undeclared), ``manages`` (sidecars reached through
    ``ctx.<name>.*``), ``timeseries`` (the declared measurements, ``None`` when
    undeclared) and ``dependencies`` (the ``plugins.invoke`` edges)."""
    egress = getattr(cls, "EGRESS", None)
    if isinstance(egress, (list, tuple, set, frozenset)):
        egress = sorted(egress)
    timeseries = getattr(cls, "TIMESERIES", None)
    measurements = (
        sorted(timeseries.get("measurements", []))
        if isinstance(timeseries, dict)
        else None
    )
    return {
        "contract_version": contract_version(plugin),
        "egress": egress,
        "manages": sorted(getattr(cls, "MANAGES", None) or []),
        "timeseries": measurements,
        "dependencies": sorted(getattr(md, "dependencies", None) or []),
    }


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
        "runtime": "python",
        "tier": compute_tier("python"),
        **_declarations(type(plugin), plugin, md),
    }


def build_catalog() -> Dict[str, Any]:
    """The full catalog manifest: one entry per plugin, sorted by name."""
    sys.path.insert(0, str(ROOT))
    entries = [_entry(pkg) for pkg in _plugin_packages()]
    entries.sort(key=lambda e: e["name"])
    # Resolve + enforce the owner-approved runner group (D1.6) before emitting.
    entries = _resolve_runner_groups(entries)
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
    try:
        rendered = _serialize(build_catalog())
    except RunnerGroupError as exc:
        print(exc, file=sys.stderr)
        return 1
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
