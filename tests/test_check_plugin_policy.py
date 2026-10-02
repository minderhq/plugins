"""Tests for scripts/check_plugin_policy.py."""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_plugin_policy", _ROOT / "scripts" / "check_plugin_policy.py"
)
assert _spec and _spec.loader
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_plugin_policy"] = policy  # dataclasses need it registered
_spec.loader.exec_module(policy)


def _rules(src: str):
    bad, _ = policy.check_source(src)
    return [v.rule for v in bad]


@pytest.mark.parametrize(
    "src, rule",
    [
        ("import subprocess\n", "subprocess"),
        ("import subprocess as sp\n", "subprocess"),
        ("from subprocess import run\n", "subprocess"),
        ("import asyncio\nasyncio.create_subprocess_exec('ls')\n", "subprocess"),
        ("import asyncio as a\na.create_subprocess_shell('ls')\n", "subprocess"),
        ("from asyncio import create_subprocess_exec\n", "subprocess"),
        ("import os\nos.system('ls')\n", "os-exec"),
        ("import os\nos.popen('ls')\n", "os-exec"),
        ("import os\nos.execv('/bin/sh', [])\n", "os-exec"),
        ("import os\nos.spawnl(0, 'x')\n", "os-exec"),
        ("import os as o\no.posix_spawn('x', [], {})\n", "os-exec"),
        ("from os import system\n", "os-exec"),
        ("eval('1')\n", "dynamic-code"),
        ("exec('x=1')\n", "dynamic-code"),
        ("compile('1', 'f', 'eval')\n", "dynamic-code"),
        ("__import__('os')\n", "dynamic-code"),
        ("import builtins\nbuiltins.eval('1')\n", "dynamic-code"),
        ("from builtins import exec\n", "dynamic-code"),
        ("import xml.etree.ElementTree as ET\n", "stdlib-xml"),
        ("from xml.etree import ElementTree\n", "stdlib-xml"),
        ("from xml.dom import minidom\n", "stdlib-xml"),
        ("import xml.dom.minidom\n", "stdlib-xml"),
        ("import xml.sax\n", "stdlib-xml"),
        ("from xml import etree\n", "stdlib-xml"),
        ("import pickle\npickle.loads(b'')\n", "unsafe-deserialization"),
        ("import pickle as p\np.load(f)\n", "unsafe-deserialization"),
        ("import marshal\nmarshal.loads(b'')\n", "unsafe-deserialization"),
        ("from pickle import Unpickler\n", "unsafe-deserialization"),
    ],
)
def test_banned_constructs_fail(src, rule):
    assert rule in _rules(src)


@pytest.mark.parametrize(
    "src",
    [
        "import re\nre.compile('x')\n",  # attribute compile, not the builtin
        "import defusedxml.ElementTree as ET\nET.fromstring('<a/>')\n",
        "import os\nos.path.join('a', 'b')\nos.environ.get('X')\n",
        "import json\njson.loads('{}')\n",
        "import pickle\npickle.dumps(1)\n",  # serialising isn't the hazard
        "import asyncio\nasyncio.sleep(1)\n",
        "x = {'eval': 1}\nprint(x['eval'])\n",
        "s = 'import subprocess; eval(1)'\n",  # strings aren't code
    ],
)
def test_safe_constructs_pass(src):
    assert _rules(src) == []


def test_allow_on_same_line_suppresses_and_is_reported():
    src = "import subprocess  # plugin-policy: allow subprocess -- needs nmap\n"
    bad, ok = policy.check_source(src)
    assert bad == []
    assert [v.rule for v in ok] == ["subprocess"]


def test_allow_on_comment_block_above_targets_next_code_line():
    src = (
        "import asyncio\n"
        "# plugin-policy: allow subprocess -- shells out to nmap\n"
        "# (continuation of the justification)\n"
        "asyncio.create_subprocess_exec('nmap')\n"
        "asyncio.create_subprocess_exec('again')\n"
    )
    bad, ok = policy.check_source(src)
    assert len(ok) == 1 and ok[0].line == 4
    assert len(bad) == 1 and bad[0].line == 5  # one line only, not a blanket


def test_allow_requires_reason():
    assert _rules("import subprocess  # plugin-policy: allow subprocess\n") == [
        "subprocess"
    ]
    assert _rules("import subprocess  # plugin-policy: allow subprocess -- \n") == [
        "subprocess"
    ]


def test_allow_is_rule_specific():
    src = "eval('1')  # plugin-policy: allow subprocess -- wrong rule\n"
    assert _rules(src) == ["dynamic-code"]


def test_allow_inside_string_is_ignored():
    src = 'eval("# plugin-policy: allow dynamic-code -- nope")\n'
    assert _rules(src) == ["dynamic-code"]


def test_main_fails_on_seeded_eval_and_passes_clean(tmp_path, capsys):
    plugin = tmp_path / "demo"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("X = 1\n")
    # test/script dirs are not plugin code and are skipped
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("eval('1')\n")
    assert policy.main([str(tmp_path)]) == 0

    (plugin / "__init__.py").write_text("X = eval('1')\n")
    assert policy.main([str(tmp_path)]) == 1
    assert "[dynamic-code]" in capsys.readouterr().out


def test_current_catalog_is_clean():
    assert policy.main([str(_ROOT)]) == 0
