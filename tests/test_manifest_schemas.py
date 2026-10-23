"""Every plugin manifest in the catalog validates against the plugin SDK's
schema: a declarative connector (``spec.runtime.kind: declarative``) against
the declarative connector schema, any other manifest against the manifest
plugin schema. This is what ``minder-plugin validate`` checks, and Minder
validates against the same schemas.

Files without ``apiVersion`` (e.g. telegraf's bundle-claim ``manifest.yml``)
are not plugin manifests and are skipped.
"""

from pathlib import Path

import pytest
import yaml
from minder_plugin_sdk import validate_manifest

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


MANIFESTS = sorted(
    p
    for p in ROOT.glob("*/manifest.*")
    if p.suffix in (".yml", ".yaml", ".json")
    and isinstance(_load(p), dict)
    and "apiVersion" in _load(p)
)


def test_the_catalog_has_declarative_manifests():
    names = {p.parent.name for p in MANIFESTS}
    assert {"arxiv", "frankfurter", "openlibrary", "wikipedia"} <= names


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_matches_the_sdk_schema(path):
    validate_manifest(_load(path))  # raises ManifestError with every problem


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_name_matches_its_directory(path):
    # Minder's runner refuses a manifest that names another directory.
    assert _load(path)["metadata"]["name"] == path.parent.name
