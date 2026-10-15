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
    "runtime",
    "tier",
    "contract_version",
    "egress",
    "manages",
    "timeseries",
    "dependencies",
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
        assert e["runtime"] in gen_catalog.RUNTIME_MIN_TIER
        assert e["tier"] in gen_catalog.TIERS
        assert (e["runtime"], e["tier"]) == ("python", "T2")


def test_contract_v2_declarations_are_in_the_catalog():
    by_name = {e["name"]: e for e in gen_catalog.build_catalog()["plugins"]}
    telegraf, network, webcrawl = (
        by_name["telegraf"],
        by_name["network"],
        by_name["webcrawl"],
    )
    assert (telegraf["contract_version"], telegraf["manages"]) == (2, ["telegraf"])
    assert telegraf["egress"] == []
    assert (network["contract_version"], network["egress"]) == (2, "lan")
    assert network["dependencies"] == ["telegraf"]
    assert (webcrawl["contract_version"], webcrawl["egress"]) == (2, "public")
    for e in by_name.values():
        # Every catalog plugin states where it connects.
        assert e["egress"] is not None, f"{e['name']} declares no EGRESS"
        assert e["contract_version"] in (1, 2)


def test_entries_are_sorted_by_name():
    names = [e["name"] for e in gen_catalog.build_catalog()["plugins"]]
    assert names == sorted(names)


def test_committed_catalog_json_is_fresh():
    # the committed catalog.json must match a fresh generation — regenerate with
    # `python scripts/gen_catalog.py` after adding or changing any plugin.
    assert (
        gen_catalog.main(["--check"]) == 0
    ), "catalog.json is stale — run gen_catalog.py"


def test_compute_tier_need_per_runtime():
    assert gen_catalog.compute_tier("declarative") == "T0"
    assert gen_catalog.compute_tier("python") == "T2"
    assert gen_catalog.compute_tier("image") == "T3"
    assert gen_catalog.compute_tier("remote") == "T4"


def test_compute_tier_floor_raises_but_never_lowers():
    assert gen_catalog.compute_tier("declarative", "T3") == "T3"
    assert gen_catalog.compute_tier("image", "T0") == "T3"
    assert gen_catalog.compute_tier("python", "T2") == "T2"


def test_compute_tier_rejects_unknown_inputs():
    import pytest

    with pytest.raises(KeyError):
        gen_catalog.compute_tier("wasm-ish")
    with pytest.raises(ValueError):
        gen_catalog.compute_tier("python", "T9")
