"""Read-only catalogue diagnosis for M7 phase 2A.

A rejected-operation receipt is only actionable if it says *which kind* of unrepresentable
the requirement hit. Four cases look identical from the outside — the element is simply not
in the drawing — and they have four different answers:

* ``visible``     the catalogue defines it and the model could see it. Not a catalogue problem.
* ``hidden``      the catalogue defines it and withholds it from the model's listing. A
                  visibility bug: if the compiler and renderer handle it, un-hiding is the fix.
* ``missing``     the catalogue does not define it. A genuine gap, which must be reported to a
                  human with alternatives rather than silently dropped, substituted, or
                  invented.
* ``unsupported`` defined and visible, but missing the geometry or ports an element needs to
                  be placeable, so the compiler or the renderer cannot use it.

This module reads only. It adds no symbol, changes no visibility, and is not imported by the
application's request paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .symbols import SymbolRegistry

AuditVerdict = Literal[
    "visible", "hidden", "missing", "compiler_unsupported", "renderer_unsupported"
]

#: The classification order is fixed: existence is asked before visibility, because
#: "hidden" is a statement about a key that exists and "missing" is a statement about one
#: that does not. Asking them in the other order is how the two got conflated.
AUDIT_ORDER: tuple[str, ...] = (
    "existence",
    "visibility",
    "compiler_support",
    "renderer_support",
)

MAX_ALTERNATIVES = 8


@dataclass(frozen=True)
class CatalogueAuditEntry:
    """One audited requirement. ``source_requirement`` records who asked for it."""

    requested_type: str
    requested_tag: str
    source_requirement: str
    verdict: AuditVerdict
    symbol_key: str | None
    available_alternatives: tuple[str, ...]
    detail: str

    def is_representable(self) -> bool:
        return self.verdict == "visible"


def _alternatives(registry: SymbolRegistry, requested_type: str) -> tuple[str, ...]:
    """Catalogue keys whose name matches the requested type, for a human to choose from.

    Offered, never applied: the phase-1 contract forbids substituting a lookalike
    automatically, because a generic pump standing in for a molecular-sieve bed is a semantic
    lie rather than an approximation.
    """

    needle = requested_type.strip()
    if not needle:
        return ()
    exact = [symbol.key for symbol in registry.list() if symbol.key == needle]
    if exact:
        return tuple(exact[:MAX_ALTERNATIVES])
    named = [
        symbol.key for symbol in registry.list() if needle in symbol.name or symbol.name in needle
    ]
    if named:
        return tuple(sorted(named)[:MAX_ALTERNATIVES])
    return ()


def audit_symbol_key(
    registry: SymbolRegistry,
    symbol_key: str,
    *,
    requested_type: str = "",
    requested_tag: str = "",
    source_requirement: str = "",
) -> CatalogueAuditEntry:
    """Classify one requested symbol key against the live catalogue."""

    requested = requested_type or symbol_key

    if not registry.exists(symbol_key):
        return CatalogueAuditEntry(
            requested_type=requested,
            requested_tag=requested_tag,
            source_requirement=source_requirement,
            verdict="missing",
            symbol_key=None,
            available_alternatives=_alternatives(registry, requested),
            detail=f"the catalogue defines no symbol {symbol_key!r}",
        )

    if registry.is_hidden(symbol_key):
        definition = registry.get(symbol_key)
        if not definition.shapes:
            verdict: AuditVerdict = "renderer_unsupported"
            detail = f"{symbol_key!r} is hidden and has no drawing shapes"
        elif not definition.ports:
            verdict = "compiler_unsupported"
            detail = f"{symbol_key!r} is hidden and has no connectable ports"
        else:
            verdict = "hidden"
            detail = (
                f"{symbol_key!r} exists and is drawable but is withheld from the model's "
                "catalogue listing"
            )
        return CatalogueAuditEntry(
            requested_type=requested,
            requested_tag=requested_tag,
            source_requirement=source_requirement,
            verdict=verdict,
            symbol_key=symbol_key,
            available_alternatives=(),
            detail=detail,
        )

    definition = registry.get(symbol_key)
    if not definition.shapes:
        return CatalogueAuditEntry(
            requested_type=requested,
            requested_tag=requested_tag,
            source_requirement=source_requirement,
            verdict="renderer_unsupported",
            symbol_key=symbol_key,
            available_alternatives=(),
            detail=f"{symbol_key!r} is visible but carries no drawing shapes",
        )
    if not definition.ports:
        return CatalogueAuditEntry(
            requested_type=requested,
            requested_tag=requested_tag,
            source_requirement=source_requirement,
            verdict="compiler_unsupported",
            symbol_key=symbol_key,
            available_alternatives=(),
            detail=f"{symbol_key!r} is visible but has no connectable ports",
        )

    return CatalogueAuditEntry(
        requested_type=requested,
        requested_tag=requested_tag,
        source_requirement=source_requirement,
        verdict="visible",
        symbol_key=symbol_key,
        available_alternatives=(),
        detail=f"{symbol_key!r} is defined, visible and placeable",
    )


def audit_for_receipt(registry: SymbolRegistry, receipt) -> CatalogueAuditEntry | None:
    """Classify a rejected-operation receipt when it names a symbol key.

    Returns ``None`` when the receipt is not about a symbol at all, so that a caller can
    distinguish "no catalogue involvement" from "catalogue involvement that came back clean".
    """

    key = ""
    values = getattr(receipt, "available_values", None) or {}
    for candidate in values.get("symbol_key", []) or []:
        key = candidate
        break
    if not key:
        return None
    return audit_symbol_key(
        registry,
        key,
        requested_type=key,
        source_requirement=getattr(receipt, "reason_code", ""),
    )


def audit_declared_requirements(
    registry: SymbolRegistry,
    requirements: list[tuple[str, str, str]],
) -> list[CatalogueAuditEntry]:
    """Audit (symbol_key, requested_tag, source_requirement) triples in order."""

    return [
        audit_symbol_key(
            registry,
            symbol_key,
            requested_tag=requested_tag,
            source_requirement=source_requirement,
        )
        for symbol_key, requested_tag, source_requirement in requirements
    ]
