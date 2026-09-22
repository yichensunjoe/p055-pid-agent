"""Interpreter-independent identity of a Python definition: a canonical projection of its AST.

Why not bytecode: ``generator_fingerprint`` digests CPython bytecode, so the same frozen corpus
publishes a different number on 3.11 and 3.12 -- provenance, not identity. Why not ``ast.dump``
either: the dump *format* moves between releases. ``FunctionDef`` grew ``type_params`` in 3.12, so the
same ``_patch`` hashes differently there::

    python3.11: 930398d67e9220f4   python3.12: a182c517faab2e95    # ast.dump

The fix is to project the tree through this module's own canonical form instead of a stdlib format:
node fields are taken in ``ast.iter_fields`` order, ``None`` and empty lists are dropped (which is
what makes a field that only exists on newer interpreters vanish when it is unused), and the result
is hashed as sorted-key JSON. The same source then hashes the same number on every interpreter, while
any change that can affect behaviour -- a statement, an expression, a constant, a decorator -- moves
it.

This module is deliberately **stdlib-only**, and that is a functional requirement rather than a
style: ``scripts/m5_closeout_identity_311_check.py`` runs on a second interpreter with no project
dependencies installed and re-derives every recorded identity from the source file alone. A
verification script that had to import the project could not run where the verification matters.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from typing import Any


def canonical_ast(node: ast.AST | list[Any] | Any) -> Any:
    """A tree-shaped projection of an AST node that no interpreter release can reword."""

    if isinstance(node, ast.AST):
        fields: dict[str, Any] = {}
        for name, value in ast.iter_fields(node):
            if value is None:
                continue
            if isinstance(value, list) and not value:
                continue
            fields[name] = canonical_ast(value)
        return {type(node).__name__: fields}
    if isinstance(node, list):
        return [canonical_ast(item) for item in node]
    return node


def ast_identity(node: ast.AST) -> str:
    """The identity of one AST node, in the canonical form above."""

    text = json.dumps(
        canonical_ast(node),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=repr,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_ast_identity(source: str) -> str:
    """The identity of a piece of source, parsed the way a function's own source is parsed.

    ``textwrap.dedent`` matches ``inspect.getsource``'s indentation for a nested definition, so the
    same function hashes the same whether it is read from the live object or from the file.
    """

    return ast_identity(ast.parse(textwrap.dedent(source)))


def module_definition_identities(source: str) -> dict[str, str]:
    """``qualname -> identity`` for every function definition in a module's source.

    ``<locals>`` qualnames are reconstructed the way CPython builds them, so a definition recorded
    from the live object (``inspect.getsource`` + ``__qualname__``) can be looked up here without
    either side knowing how the other walked the tree.
    """

    return {
        qualname: ast_identity(ast.Module(body=[node], type_ignores=[]))
        for qualname, node in _walk(ast.parse(textwrap.dedent(source)).body)
    }


def _walk(body: list[ast.stmt], prefix: str = ""):
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        qualname = f"{prefix}.<locals>.{node.name}" if prefix else node.name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield qualname, node
        yield from _walk(node.body, qualname)


__all__ = [
    "ast_identity",
    "canonical_ast",
    "module_definition_identities",
    "source_ast_identity",
]
