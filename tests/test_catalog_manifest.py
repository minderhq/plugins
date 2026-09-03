"""The generated catalog.json manifest stays complete + fresh.

scripts/ isn't a package (gen_catalog does a bare `import <plugin>` expecting the
repo root on sys.path), so it's loaded by path like the plugins themselves.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import gen_catalog  # noqa: E402

_REQUIRED = (
    "package",
    "name",
    "version",
    "description",
    "author",
    "capabilities",
    "requires",
    "config_schema",
    "ui_schema",
    "ai_tools",
    "display",
)


def test_build_catalog_covers_every_plugin():
    cat = gen_catalog.build_catalog()
    assert cat["source"] == "minderhq/plugins"
    assert cat["count"] == len(gen_catalog._plugin_packages()) >= 8
    names = {e["name"] for e in cat["plugins"]}
    # a couple of well-known ones must be present
    assert {"crypto", "github", "weather"} <= names


def test_every_entry_has_the_required_fields():
    for e in gen_catalog.build_catalog()["plugins"]:
        for field in _REQUIRED:
            assert field in e, f"{e.get('name')!r} missing {field!r}"
        assert e["name"] and e["version"], f"incomplete metadata: {e!r}"
        assert isinstance(e["capabilities"], list)
        assert isinstance(e["ai_tools"], list)


def test_entries_are_sorted_by_name():
    names = [e["name"] for e in gen_catalog.build_catalog()["plugins"]]
    assert names == sorted(names)


def test_committed_catalog_json_is_fresh():
    # the committed catalog.json must match a fresh generation — regenerate with
    # `python scripts/gen_catalog.py` after adding or changing any plugin.
    assert (
        gen_catalog.main(["--check"]) == 0
    ), "catalog.json is stale — run gen_catalog.py"
