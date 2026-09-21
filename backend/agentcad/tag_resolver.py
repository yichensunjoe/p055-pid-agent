"""The one place that answers "what is this symbol's tag?" (M4 regression fix).

Why this module exists
----------------------

The production write path runs ``polish_full_diagram_transaction`` when a transaction
covers the whole diagram. The polish turns each symbol's fixed ``label`` into an
editable ``symbol_label`` annotation and then **clears ``symbol.label``**, so that a
symbol has one editable text and no second, uneditable copy of the same string.

The canonical rules never learned that. ``TAG_MISSING`` and ``TAG_DUPLICATE`` read
``symbol.label`` directly, which means that on any drawing the product itself just
created, every symbol looks untagged and a duplicate tag cannot be expressed at all.
That is a canonical conclusion disagreeing with the drawing it describes, and it is a
bug in the rules, not in the polish.

What it is *not* allowed to be
------------------------------

A second source of truth. Validators must not each grow their own fallback chain, and
the resolver must not write to the document: it is pure, read-only, and deterministic.
Resolving a tag is a *reading* of the drawing, and reading a drawing never changes it.

Precedence
----------

``properties.tag`` → ``symbol.label`` → ``symbol_label`` annotation text

Each level contributes only a non-empty string after ``strip()``. ``properties.tag``
stays first because it is the semantic field the compiler and the API write; ``label``
stays second so that a drawing which never went through polish keeps behaving exactly
as it did before this fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Document, SymbolElement

#: The annotation role the polish writes. An annotation only counts as a symbol's tag
#: when it says it is one *and* names the symbol as its subject; the virtual label the
#: annotation-quality rules synthesise to measure overlap deliberately has no role and
#: is therefore not a tag source.
SYMBOL_LABEL_ANNOTATION_ROLE = "symbol_label"

TagSource = Literal["properties.tag", "symbol.label", "symbol_label_annotation", "none"]

SOURCE_PROPERTIES_TAG: TagSource = "properties.tag"
SOURCE_SYMBOL_LABEL: TagSource = "symbol.label"
SOURCE_ANNOTATION: TagSource = "symbol_label_annotation"
SOURCE_NONE: TagSource = "none"


@dataclass(frozen=True)
class SymbolTag:
    """A resolved tag plus where it came from, so a report can explain itself."""

    tag: str
    source: TagSource

    #: Non-empty annotation texts that disagree with each other, sorted. Empty in the
    #: ordinary case. This is the one state the resolver refuses to *resolve*: two
    #: different strings on the same subject is a drawing problem, and this fix does
    #: not invent a finding code to express it (baseline §E1 forbids widening M4's rule
    #: system). What it does do is stay deterministic — see :func:`describe_symbol_tag`.
    conflicting_annotations: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tag)


def _annotation_texts(document: Document, symbol_id: str) -> list[str]:
    """The symbol's ``symbol_label`` annotations, in document order, stripped and non-empty."""

    texts: list[str] = []
    for element in document.elements:
        if element.type != "text":
            continue
        metadata = element.metadata or {}
        if (
            metadata.get("parent_element_id") != symbol_id
            or metadata.get("annotation_role") != SYMBOL_LABEL_ANNOTATION_ROLE
        ):
            continue
        text = (element.text or "").strip()
        if text:
            texts.append(text)
    return texts


def describe_symbol_tag(document: Document, symbol: SymbolElement) -> SymbolTag:
    """Resolve one symbol's tag and name the level it came from.

    The annotation level is resolved deterministically: repetitions of the same text
    resolve to that text, and several *different* texts resolve to their smallest one
    (never to whichever happened to be first in the element list). The conflicting set
    is carried on the result so the abnormal state is reportable instead of invisible.
    """

    from_properties = str((symbol.properties or {}).get("tag") or "").strip()
    if from_properties:
        return SymbolTag(tag=from_properties, source=SOURCE_PROPERTIES_TAG)

    from_label = (symbol.label or "").strip()
    if from_label:
        return SymbolTag(tag=from_label, source=SOURCE_SYMBOL_LABEL)

    distinct = sorted(set(_annotation_texts(document, symbol.id)))
    if len(distinct) == 1:
        return SymbolTag(tag=distinct[0], source=SOURCE_ANNOTATION)
    if len(distinct) > 1:
        return SymbolTag(
            tag=distinct[0], source=SOURCE_ANNOTATION, conflicting_annotations=tuple(distinct)
        )
    return SymbolTag(tag="", source=SOURCE_NONE)


def resolve_symbol_tag(document: Document, symbol: SymbolElement) -> str:
    """The canonical tag text for one symbol, or ``""`` when the drawing gives none."""

    return describe_symbol_tag(document, symbol).tag


__all__ = [
    "SOURCE_ANNOTATION",
    "SOURCE_NONE",
    "SOURCE_PROPERTIES_TAG",
    "SOURCE_SYMBOL_LABEL",
    "SYMBOL_LABEL_ANNOTATION_ROLE",
    "SymbolTag",
    "TagSource",
    "describe_symbol_tag",
    "resolve_symbol_tag",
]
