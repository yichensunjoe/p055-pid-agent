"""The vocabulary a drawing sentence is read with, and the candidate sets code builds from it.

Two planners read engineering sentences: the legacy semantic planner (which produces editable
operations for an existing drawing) and the M7 planner (which produces a :class:`DiagramSpec`).
They disagree about almost everything -- what the output is, where geometry comes from, whether the
model chooses a position -- but they must agree about **what a phrase means as a lookup**, or the
same sentence would name two different devices depending on which door it came in.

So this module owns exactly the model-independent half:

* how a sentence is split and a clause classified (a string test, not a judgment),
* the phrase -> catalogue hint table,
* which symbols a clause could mean,
* which ordered pairs of already-declared devices a connection clause could join.

Nothing here calls a model and nothing here decides: it builds the candidate sets that the
judgments choose between, and it is deliberately free of both planners' output types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .symbols import SymbolRegistry

#: Phrase -> catalogue hint. The map is *code*, not a model decision, because it is a lookup: when a
#: clause says 「泵」 the candidate set is the pump symbols, and asking a model to find that would be
#: a slower way to spell a dictionary. The judgment is used where a lookup cannot decide.
SYMBOL_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("泵", "pump", "blower", "压缩机", "风机"), ("pump", "blower", "compressor", "fan")),
    (("阀", "valve", "fcv", "pcv"), ("valve",)),
    (("罐", "tank", "vessel", "容器", "槽"), ("tank", "vessel", "drum")),
    (("塔", "column", "tower"), ("column", "tower")),
    (("换热", "冷却", "加热", "exchanger", "cooler", "heater"), ("exchanger", "cooler", "heater")),
    (("流量", "flow"), ("flow",)),
    (("仪表", "变送", "transmitter", "instrument"), ("instrument", "transmitter", "sensor")),
    (("过滤", "filter", "床"), ("filter", "bed")),
)

ADD_VERBS = (
    "添加", "新增", "加一个", "加个", "放置", "放一个", "画一个", "画个", "建立", "新建",
    "add", "place", "draw", "create",
)
CONNECT_VERBS = (
    "接到", "连到", "连接到", "连接", "接入", "管线", "管道", "连线",
    "connect", "pipe", "line to",
)
#: Removal clauses name what goes away. A removal target is data, not a judgment: the tag
#: the sentence spells out is looked up in the current spec, and a tag that is not there is
#: receipted, never guessed.
REMOVE_VERBS = (
    "删除", "删掉", "去掉", "移除", "拿走",
    "remove", "delete",
)
CLAUSE_BREAK = re.compile(r"[，,;；。\n]+")

#: An engineering tag written in the sentence: ``V-101``, ``PT-101``, ``X-301A``. Read by code
#: because it is a pattern, not a semantic question -- a tag the user spells out is data.
TAG_PATTERN = re.compile(r"[A-Za-z]{1,5}-?\d{1,5}[A-Za-z]?")

#: How many candidates go into one judgment. System One compares options *inside* one question, and a
#: list of two dozen near-synonyms makes the distribution meaningless rather than more precise.
MAX_CANDIDATES = 24
MAX_CONNECTION_CANDIDATES = 12


@dataclass(frozen=True)
class Clause:
    text: str
    kind: str  # "add" | "connect" | "unknown"


@dataclass(frozen=True)
class SymbolCandidate:
    key: str
    name: str
    category: str
    description: str


def normalise(text: str) -> str:
    return text.casefold().strip()


def split_clauses(prompt: str) -> list[Clause]:
    """Split the sentence and classify each clause by verb -- a string test, not a model call."""

    clauses: list[Clause] = []
    for raw in CLAUSE_BREAK.split(prompt or ""):
        text = raw.strip()
        if not text:
            continue
        lowered = normalise(text)
        if any(verb in lowered for verb in CONNECT_VERBS):
            clauses.append(Clause(text=text, kind="connect"))
        elif any(verb in lowered for verb in ADD_VERBS):
            clauses.append(Clause(text=text, kind="add"))
        elif any(verb in lowered for verb in REMOVE_VERBS):
            clauses.append(Clause(text=text, kind="remove"))
        else:
            clauses.append(Clause(text=text, kind="unknown"))
    return clauses


def extract_tags(clause: str) -> list[str]:
    """The tags the clause spells out, in order of appearance, upper-cased and de-duplicated.

    Order is kept rather than sorted: 「把 P-101 接到 V-101」 names a source and a target, and the
    order they were written in is the only thing in the sentence that says which is which.
    """

    seen: list[str] = []
    for match in TAG_PATTERN.finditer(clause or ""):
        tag = match.group(0).upper()
        if tag not in seen:
            seen.append(tag)
    return seen


def all_symbol_candidates(registry: SymbolRegistry, *, limit: int = MAX_CANDIDATES) -> list[SymbolCandidate]:
    return [
        SymbolCandidate(
            key=definition.key,
            name=definition.name,
            category=definition.category,
            description=definition.description,
        )
        for definition in registry.list()
    ][:limit]


def matched_hints(clause: str) -> tuple[str, ...]:
    """The catalogue hint words the clause's device words map to, empty when it names no kind.

    Exposed separately from :func:`candidate_symbols` because a matched hint with zero catalogue
    rows is a *catalogue gap*, not an invitation to widen the question: the caller must report
    the gap rather than fall back to the whole catalogue.
    """

    lowered = normalise(clause)
    wanted: list[str] = []
    for phrases, hints in SYMBOL_HINTS:
        if any(phrase in lowered for phrase in phrases):
            wanted.extend(hints)
    return tuple(wanted)


def candidate_symbols(
    registry: SymbolRegistry, clause: str, *, limit: int = MAX_CANDIDATES
) -> list[SymbolCandidate]:
    """The symbols the clause could mean, narrowed by the hint table and then by name.

    A matched hint with zero rows returns *no* candidates: that is a catalogue gap, and the
    whole catalogue is never offered as a substitute. Choosing a look-alike the sentence did
    not ask for is the substitution the phase-1 rules forbid; the gap is reported by the
    caller instead, with ``available_alternatives`` for a human to read.
    """

    wanted = matched_hints(clause)
    rows: list[SymbolCandidate] = []
    for definition in registry.list():
        haystack = normalise(f"{definition.key} {definition.name} {definition.category}")
        if wanted and not any(hint in haystack for hint in wanted):
            continue
        rows.append(
            SymbolCandidate(
                key=definition.key,
                name=definition.name,
                category=definition.category,
                description=definition.description,
            )
        )
    return rows[:limit]


def available_alternatives(
    registry: SymbolRegistry, *, limit: int = MAX_CANDIDATES
) -> list[SymbolCandidate]:
    """What the catalogue does carry, for a gap receipt to show a human. Never a question."""

    return all_symbol_candidates(registry, limit=limit)


__all__ = [
    "ADD_VERBS",
    "CLAUSE_BREAK",
    "CONNECT_VERBS",
    "REMOVE_VERBS",
    "MAX_CANDIDATES",
    "MAX_CONNECTION_CANDIDATES",
    "SYMBOL_HINTS",
    "TAG_PATTERN",
    "Clause",
    "SymbolCandidate",
    "all_symbol_candidates",
    "candidate_symbols",
    "extract_tags",
    "normalise",
    "split_clauses",
    "available_alternatives",
    "matched_hints",
]
