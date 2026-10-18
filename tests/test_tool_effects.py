"""Every AI tool in the catalog declares its effect class (plugin-sdk#50).

``minder-plugin validate`` only *warns* about a tool with no ``effect`` or a
declaration that the tool's name, method or arguments contradict, so CI would
pass with either. This makes both a failure, and pins the tools that are not
plain reads so a change to them is a deliberate one.
"""

import importlib

import pytest

from minder_plugin_sdk.tool_effects import tool_effect_warnings

from test_catalog import _plugin_class, _plugin_packages

# Every tool not listed here is a pure read.
_NOT_READ = {("network", "network_reconcile"): "destructive"}


@pytest.mark.parametrize("pkg", _plugin_packages())
def test_tools_declare_an_uncontradicted_effect(pkg):
    plugin = _plugin_class(importlib.import_module(pkg))()
    assert tool_effect_warnings(plugin) == []


@pytest.mark.parametrize("pkg", _plugin_packages())
def test_reads_are_idempotent_and_the_rest_are_pinned(pkg):
    cls = _plugin_class(importlib.import_module(pkg))
    for tool in getattr(cls, "AI_TOOLS", []) or []:
        expected = _NOT_READ.get((pkg, tool["name"]), "read")
        assert tool["effect"] == expected, (pkg, tool["name"])
        if expected == "read":
            assert tool.get("idempotent") is True, (pkg, tool["name"])
