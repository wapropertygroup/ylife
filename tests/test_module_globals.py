"""No module-level constant may be used without being defined.

``/api/options/<ticker>`` returned 500 to every visitor for several commits
because ``fe5ea3b`` deleted ``_OPTIONS_CACHE``, ``_OPTIONS_CACHE_LOCK`` and
``_OPTIONS_CACHE_TTL`` while leaving all five uses of them in place. Nothing
catches that: Python binds a global when the line *runs*, not when the module is
imported, so the app started cleanly, every other route worked, and the only
evidence was a ``NameError`` traceback in the journal that nobody reads. It was
found from nginx's access log, days later.

The check is deliberately narrow — module-level ``_UPPER_CASE`` names only —
because that is the shape of every cache, lock, TTL and tunable in these files,
and it is the shape a stray deletion leaves dangling. Widening it to all names
would need real scope analysis to avoid drowning in false positives from
comprehensions and closures, and the noise would get the test deleted.

Complements ``tests/test_import_graph.py``: that one catches a module that
cannot be imported, this one catches a module that imports fine and raises the
first time a particular line is reached.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "ystocker"

#: Names that look like module constants but are bound somewhere this crude
#: walk cannot see -- `global X` inside a function is the real one, and a few
#: are supplied by the environment rather than the file.
_ALLOWED: frozenset[str] = frozenset()


class _Collector(ast.NodeVisitor):
    """Module-level bindings, and every `_UPPER` name read anywhere."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.used: dict[str, int] = {}
        self._depth = 0

    # -- bindings ---------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        if self._depth == 0:
            for target in node.targets:
                self._bind(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._depth == 0:
            self._bind(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self._depth == 0:
            self._bind(node.target)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        # `global X` inside a function is a module-level binding wherever the
        # assignment physically sits, so honour the declaration itself.
        self.bound.update(node.names)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bound.add((alias.asname or alias.name).split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.bound.add(alias.asname or alias.name)
        self.generic_visit(node)

    def _enter(self, node) -> None:
        self.bound.add(node.name) if self._depth == 0 else None
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _bind(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element)

    # -- uses -------------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and _looks_like_a_constant(node.id):
            self.used.setdefault(node.id, node.lineno)
        elif isinstance(node.ctx, ast.Store):
            # A local of the same name inside a function is a binding for our
            # purposes: it means the read is satisfied without a global.
            self.bound.add(node.id)
        self.generic_visit(node)


def _looks_like_a_constant(name: str) -> bool:
    """`_UPPER_CASE` or `UPPER_CASE`, at least two characters."""
    stripped = name.lstrip("_")
    return (len(stripped) > 1 and stripped.isupper()
            and stripped.replace("_", "").isalnum())


def _undefined(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    collector = _Collector()
    collector.visit(tree)
    return [f"{path.name}:{line} {name}"
            for name, line in sorted(collector.used.items())
            if name not in collector.bound
            and name not in _ALLOWED
            and not hasattr(__builtins__, name)]


class ModuleConstantTests(unittest.TestCase):

    def test_the_walk_actually_sees_something(self):
        """Guards the collector: a visitor that bound nothing would make every
        assertion below fail loudly, and one that *used* nothing would make
        them all pass vacuously."""
        tree = ast.parse((PKG / "routes.py").read_text())
        collector = _Collector()
        collector.visit(tree)
        self.assertGreater(len(collector.bound), 200)
        self.assertGreater(len(collector.used), 50)

    def test_every_module_constant_used_is_defined(self):
        offenders: list[str] = []
        for path in sorted(PKG.glob("*.py")):
            offenders.extend(_undefined(path))
        self.assertEqual(
            offenders, [],
            "used but never bound at module scope — Python raises NameError the "
            "first time the line runs, not at import, so this 500s one endpoint "
            f"and nothing else: {offenders}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
