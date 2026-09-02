"""The deferred init hands the symbol index to every mounted adapter.

A regression test for a bug that was invisible in exactly the way that
matters: ``_heavy_init`` referenced ``capabilities`` without importing it
into *its* scope — the name is bound inside ``main()``, a different
function — so the whole deferred initialisation died on a ``NameError``.

The symptom was not an error anybody would chase. One traceback at
startup, and then every hover, definition and reference answered "no
answer" for the life of the session, because that is the honest thing an
adapter with no index says. It reads as a slow or empty index rather than
as a crash.

Found on 2026-09-02 by running the app rather than the suite, which is
the point of this file: the attachment loop is startup plumbing that no
unit test was exercising.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from aic_dc import main as main_module


def _function(name: str) -> ast.FunctionDef:
    source = Path(inspect.getfile(main_module)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"{name} is no longer defined in main.py")


def _bound_names(node: ast.AST) -> set[str]:
    """Every name the function binds — imports, assignments, arguments."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in child.names)
        elif isinstance(child, ast.ImportFrom):
            names.update(a.asname or a.name for a in child.names)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
    return names


def test_heavy_init_binds_every_module_it_uses():
    """The check the NameError would have failed.

    Deliberately structural rather than a mocked call: reproducing the
    failure needs the real deferred-init path, and asserting on the
    syntax tree catches it without standing up an index, a repo and a
    server.
    """
    node = _function("_heavy_init")
    bound = _bound_names(node)
    used = {
        child.value.id
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
    }
    # Only modules this project owns; builtins and locals are not the risk.
    ours = {"capabilities", "engine_router"}
    missing = sorted((used & ours) - bound)
    assert not missing, (
        f"_heavy_init uses {missing} without binding it in its own scope. "
        "The name is bound in main(), which is a different function — the "
        "result is a NameError that takes the whole deferred init down and "
        "leaves every adapter without a symbol index."
    )


def test_the_index_reaches_every_adapter_not_just_the_first():
    """One index, handed to all of them.

    A second engine building its own index over the same tree is the
    duplication the adapter was written to avoid, and an engine that
    never received one answers every hover with "no answer" after a
    switch — silently.
    """
    source = ast.unparse(_function("_heavy_init"))
    assert "_attach_symbol_index" in source
    assert "other_engines" in source, (
        "the attachment loop no longer covers the non-master engines"
    )
