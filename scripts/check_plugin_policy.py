"""AST security policy for catalog plugins (minderhq/minder#2071, G15).

Catalog plugins run **in-process** inside minder's plugin-registry, so a plugin
that shells out, evaluates strings, parses XML with the stdlib or unpickles data
is a compromise of core, not of one plugin. This stdlib-only checker walks every
plugin ``.py`` file and fails on the constructs below -- the mechanical subset
of the ``minder-authoring:plugin-security-reviewer`` rules ("no arbitrary code
execution", "XML must use defusedxml").

Rules (id -> what fails):

* ``subprocess``   -- ``import subprocess`` / ``from subprocess import ...``,
  ``asyncio.create_subprocess_exec`` / ``asyncio.create_subprocess_shell``
* ``os-exec``      -- ``os.system`` / ``os.popen`` / ``os.exec*`` /
  ``os.spawn*`` / ``os.posix_spawn*`` / ``os.fork*`` (call or from-import)
* ``dynamic-code`` -- builtin ``eval`` / ``exec`` / ``compile`` / ``__import__``
* ``stdlib-xml``   -- any ``xml`` stdlib import (``xml.etree``, ``xml.dom``,
  ``xml.sax``, ``xml.parsers``...); use ``defusedxml`` instead
* ``unsafe-deserialization`` -- ``pickle`` / ``marshal`` / ``cPickle`` /
  ``_pickle`` ``load``/``loads``/``Unpickler``

Unguarded outbound fetches are NOT checked mechanically: the catalog has no
shared SSRF-guard helper to allowlist (``news`` and ``webcrawl`` each carry
their own guard), so that rule stays with human/agent review.

Escape hatch -- narrow and explicit, one line at a time. Put this comment on
the flagged line, or on a comment-only line directly above it (it may start a
contiguous comment block; it then covers the first code line after it)::

    # plugin-policy: allow <rule> -- <reason>

The reason is mandatory; an allow without one does not suppress anything.
Every honoured allow is printed so reviewers see them in the CI log.

Usage: ``python scripts/check_plugin_policy.py [paths...]`` (default: repo
root). Exit 0 = clean, 1 = violations.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

RULES = (
    "subprocess",
    "os-exec",
    "dynamic-code",
    "stdlib-xml",
    "unsafe-deserialization",
)

# Top-level dirs that are not plugin code loaded into core.
EXCLUDED_DIRS = {"tests", "scripts", "build", "dist", "venv", "__pycache__"}

_ALLOW_RE = re.compile(r"#\s*plugin-policy:\s*allow\s+([a-z-]+)\s+--\s+(\S.*)$")

_DYNAMIC_BUILTINS = {"eval", "exec", "compile", "__import__"}
_ASYNCIO_SUBPROCESS = {"create_subprocess_exec", "create_subprocess_shell"}
_OS_EXEC_EXACT = {"system", "popen"}
_OS_EXEC_PREFIXES = ("exec", "spawn", "posix_spawn", "fork")
_PICKLE_MODULES = {"pickle", "cPickle", "_pickle", "marshal"}
_PICKLE_NAMES = {"load", "loads", "Unpickler"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def _is_os_exec(name: str) -> bool:
    return name in _OS_EXEC_EXACT or name.startswith(_OS_EXEC_PREFIXES)


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: List[Tuple[int, str, str]] = []
        # local alias -> real module name, for `import os as o; o.system()`
        self.modules: Dict[str, str] = {}

    def _add(self, node: ast.AST, rule: str, msg: str) -> None:
        self.found.append((getattr(node, "lineno", 0), rule, msg))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.modules[alias.asname or root] = alias.name if alias.asname else root
            if root == "subprocess":
                self._add(node, "subprocess", f"import {alias.name}")
            elif root == "xml":
                self._add(node, "stdlib-xml", f"import {alias.name} (use defusedxml)")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        root = mod.split(".")[0]
        names = [a.name for a in node.names]
        if node.level == 0:
            if root == "subprocess":
                self._add(node, "subprocess", f"from {mod} import ...")
            elif root == "xml":
                self._add(node, "stdlib-xml", f"from {mod} import ... (use defusedxml)")
            elif mod == "os":
                for n in names:
                    if _is_os_exec(n):
                        self._add(node, "os-exec", f"from os import {n}")
            elif mod == "asyncio":
                for n in names:
                    if n in _ASYNCIO_SUBPROCESS or n == "subprocess":
                        self._add(node, "subprocess", f"from asyncio import {n}")
            elif mod == "builtins":
                for n in names:
                    if n in _DYNAMIC_BUILTINS:
                        self._add(node, "dynamic-code", f"from builtins import {n}")
            elif mod in _PICKLE_MODULES:
                for n in names:
                    if n in _PICKLE_NAMES:
                        self._add(
                            node, "unsafe-deserialization", f"from {mod} import {n}"
                        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DYNAMIC_BUILTINS:
            self._add(node, "dynamic-code", f"{func.id}() call")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            mod = self.modules.get(func.value.id)
            attr = func.attr
            if mod == "os" and _is_os_exec(attr):
                self._add(node, "os-exec", f"os.{attr}() call")
            elif mod == "asyncio" and attr in _ASYNCIO_SUBPROCESS:
                self._add(node, "subprocess", f"asyncio.{attr}() call")
            elif mod == "builtins" and attr in _DYNAMIC_BUILTINS:
                self._add(node, "dynamic-code", f"builtins.{attr}() call")
            elif mod in _PICKLE_MODULES and attr in _PICKLE_NAMES:
                self._add(node, "unsafe-deserialization", f"{mod}.{attr}() call")
        self.generic_visit(node)


def _allows(source: str) -> Dict[int, Set[str]]:
    """Map line -> rules allowed on it (honouring the comment-line-above form)."""
    allowed: Dict[int, Set[str]] = {}
    lines = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return allowed
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        m = _ALLOW_RE.search(tok.string)
        if not m or m.group(1) not in RULES:
            continue
        row = tok.start[0]
        own_line = lines[row - 1][: tok.start[1]].strip() == ""
        target = row
        if own_line:
            # the allow may open a multi-line comment block; it targets the
            # first code line after that block
            target = row + 1
            while target <= len(lines) and lines[target - 1].lstrip().startswith("#"):
                target += 1
        allowed.setdefault(target, set()).add(m.group(1))
    return allowed


def check_source(
    source: str, path: str = "<string>"
) -> Tuple[List[Violation], List[Violation]]:
    """Return (violations, allowed) for one file's source."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "syntax", str(exc))], []
    visitor = _Visitor()
    visitor.visit(tree)
    allowed_map = _allows(source)
    bad: List[Violation] = []
    ok: List[Violation] = []
    for line, rule, msg in visitor.found:
        v = Violation(path, line, rule, msg)
        (ok if rule in allowed_map.get(line, set()) else bad).append(v)
    return bad, ok


def iter_plugin_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).parts
        if any(part in EXCLUDED_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        yield p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    roots = [Path(a) for a in args] or [Path(__file__).resolve().parent.parent]
    bad: List[Violation] = []
    ok: List[Violation] = []
    for root in roots:
        files = [root] if root.is_file() else list(iter_plugin_files(root))
        for f in files:
            try:
                shown = str(f.resolve().relative_to(Path.cwd().resolve()))
            except ValueError:
                shown = str(f)
            b, o = check_source(f.read_text(encoding="utf-8"), shown)
            bad += b
            ok += o
    for v in ok:
        print(f"allowed: {v}")
    for v in bad:
        print(f"VIOLATION: {v}")
    if bad:
        print(
            f"\nplugin-policy: {len(bad)} violation(s). Plugins run in-process in "
            "minder core; see scripts/check_plugin_policy.py for the rules and the "
            "'# plugin-policy: allow <rule> -- <reason>' escape hatch."
        )
        return 1
    print(f"plugin-policy: OK ({len(ok)} explicit allow(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
