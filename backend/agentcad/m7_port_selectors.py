"""Deterministic port selection for the natural-language surfaces -- no guessing, no model.

When a connection clause omits the port and the symbol offers more than one compatible
port, the engine refuses to derive one (``a unique inference is a derivation``). M7-Q2
makes that refusal recoverable: the sentence may carry a *selector* -- 「把 P-101 接到
V-101 的顶部管口」 -- which resolves to a concrete ``port_id`` through a closed,
declared predicate vocabulary and the port-local facts of the frozen geometry. The
model never sees a port question; the selector is parsed by code, and every parse step
is a predicate over facts the freeze already froze.

Two layers, frozen by the design gate:

* Layer 1 -- one global closed predicate vocabulary (side / direction / process
  semantics), declared as data below. Parsing is longest-token matching against this
  table; there is no arbitrary substring guessing.
* Layer 2 -- port-local facts: the exact ``port_id`` is always a legal selector, and
  the normalized tokens of the port's own name address it when the catalogue named it
  descriptively (塔顶气相出口, 壳程入口, ...).

Resolution is predicate conjunction: a port must satisfy every parsed predicate and be
direction-compatible with its role. Exactly one survivor binds; zero and many are both
reported, never resolved by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .symbols import port_side

#: Records which deterministic selector rules interpreted the sentence. Provenance,
#: not an identity axis: it never enters any layout or materialization digest.
PORT_SELECTOR_CONTRACT_VERSION = "m7-port-selector/1"

REASON_MISSING_SELECTOR = "missing_selector"
REASON_NO_MATCH = "selector_no_match"
REASON_STILL_AMBIGUOUS = "selector_still_ambiguous"


@dataclass(frozen=True)
class SelectorPredicate:
    """One conjunct of a parsed selector."""

    kind: str  # "side" | "direction" | "semantic" | "port_id" | "name_token"
    value: str


@dataclass(frozen=True)
class ParsedSelector:
    """What the sentence's selector phrase means, as data."""

    raw: str
    predicates: tuple[SelectorPredicate, ...]

    def model(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "predicates": [
                {"kind": p.kind, "value": p.value} for p in self.predicates
            ],
        }


#: Layer 1 -- the one global closed vocabulary. Each token maps to exactly one
#: predicate; longest match wins at parse time. New entries extend this table, never
#: ad-hoc matching.
_SIDE_TOKENS = {
    "顶部管口": ("side", "top"), "塔顶": ("side", "top"), "顶部": ("side", "top"),
    "上面": ("side", "top"), "顶上": ("side", "top"), "上端": ("side", "top"),
    "底部管口": ("side", "bottom"), "塔底": ("side", "bottom"), "底部": ("side", "bottom"),
    "下面": ("side", "bottom"), "底下": ("side", "bottom"), "下端": ("side", "bottom"),
    "左侧": ("side", "left"), "左边": ("side", "left"), "左端": ("side", "left"),
    "右侧": ("side", "right"), "右边": ("side", "right"), "右端": ("side", "right"),
}
_DIRECTION_TOKENS = {
    "进口": ("direction", "in"), "入口": ("direction", "in"), "进料": ("direction", "in"),
    "进料口": ("direction", "in"), "吸入": ("direction", "in"), "吸入口": ("direction", "in"),
    "来料": ("direction", "in"), "大端": ("direction", "in"),
    "出口": ("direction", "out"), "出料": ("direction", "out"), "出料口": ("direction", "out"),
    "排出": ("direction", "out"), "排出口": ("direction", "out"), "小端": ("direction", "out"),
}
_SEMANTIC_TOKENS = {
    "气相": ("semantic", "gas"), "气体": ("semantic", "gas"),
    "液相": ("semantic", "liquid"), "液体": ("semantic", "liquid"),
    "公用工程": ("semantic", "utility"), "冷却介质": ("semantic", "utility"),
    "冷却空气": ("semantic", "cooling_air"), "空气": ("semantic", "cooling_air"),
    "排污": ("semantic", "drain"), "排液": ("semantic", "drain"), "排水": ("semantic", "drain"),
    "排尘": ("semantic", "drain"), "排渣": ("semantic", "drain"), "排放": ("semantic", "drain"),
    "放空": ("semantic", "vent"), "泄放": ("semantic", "relief"),
    "冷凝": ("semantic", "condensate"),
    "管程": ("semantic", "tube"), "壳程": ("semantic", "shell"),
    "热侧": ("semantic", "side_a"), "冷侧": ("semantic", "side_b"),
    "回流": ("semantic", "reflux"), "原料": ("semantic", "feed"), "侧线": ("semantic", "side_draw"),
    "支路": ("semantic", "branch"), "溢流": ("semantic", "overflow"),
    "切向": ("semantic", "tangential"), "主驱动": ("semantic", "motive"),
    "抽吸": ("semantic", "suction"), "混合": ("semantic", "mixture"),
    "受压": ("semantic", "pressurized"), "引线": ("semantic", "leader"),
}

_TOKEN_TABLES = (_SIDE_TOKENS, _DIRECTION_TOKENS, _SEMANTIC_TOKENS)
_PORT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_SUFFIXES = ("口", "接口", "端口", "管口", "引脚")


def normalize_name_tokens(name: str) -> tuple[str, ...]:
    """The lexical tokens a port's own name contributes -- the Layer-2 address."""

    text = name.strip()
    for suffix in _NAME_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    parts = re.split(r"[·\s／/（）()]+", text)
    return tuple(part for part in parts if part)


#: Layer-1 tokens longest-first, so 「顶部气相」 segments as 顶部 + 气相 and never as
#: 顶 + 部气 + 相. Segmentation is over the whole phrase; characters no vocabulary token
#: claims accumulate into one name predicate (Layer 2).
_SEGMENT_TOKENS: tuple[tuple[str, tuple[str, str]], ...] = tuple(
    sorted(
        ((token, mapping) for table in _TOKEN_TABLES for token, mapping in table.items()),
        key=lambda item: -len(item[0]),
    )
)


def parse_selector(raw: str) -> ParsedSelector | None:
    """The selector phrase as conjunctive predicates, or ``None`` when the sentence
    carries no selector at all.

    Longest-match segmentation against the closed Layer-1 tables -- no arbitrary
    substring guessing: a character joins a name predicate only when no declared token
    claims it. Runs of identifier-shaped characters are port_id predicates -- the exact
    port id is always a legal, deterministic selector.
    """

    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"[\s,，、的之]+", "", text)
    _ALL_VOCAB_TOKENS = frozenset(token for token, _ in _SEGMENT_TOKENS)
    if text not in _ALL_VOCAB_TOKENS:
        # A whole-phrase vocabulary token (进口, 顶部管口's parts...) is never a suffix
        # casualty; stripping applies only when the phrase itself claims no token.
        for suffix in _NAME_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                break
    predicates: list[SelectorPredicate] = []
    buffer = ""
    index = 0
    while index < len(text):
        matched = next(
            (
                token
                for token, _mapping in _SEGMENT_TOKENS
                if text.startswith(token, index)
            ),
            None,
        )
        if matched is not None:
            if buffer:
                _flush_buffer(predicates, buffer)
                buffer = ""
            kind, value = next(
                mapping for token, mapping in _SEGMENT_TOKENS if token == matched
            )
            predicates.append(SelectorPredicate(kind, value))
            index += len(matched)
        else:
            buffer += text[index]
            index += 1
    if buffer:
        _flush_buffer(predicates, buffer)
    deduped = tuple(dict.fromkeys(predicates))
    return ParsedSelector(raw=raw.strip(), predicates=deduped)


def _flush_buffer(predicates: list[SelectorPredicate], buffer: str) -> None:
    if _PORT_ID_PATTERN.match(buffer):
        predicates.append(SelectorPredicate("port_id", buffer))
    else:
        predicates.append(SelectorPredicate("name_token", buffer))


@dataclass(frozen=True)
class PortCandidate:
    """One compatible port as the receipt reports it: facts only, no coordinates."""

    port_id: str
    name: str
    direction: str
    medium: str
    side: str
    selectors: tuple[str, ...]

    def model(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "name": self.name,
            "direction": self.direction,
            "medium": self.medium,
            "side": self.side,
            "selectors": list(self.selectors),
        }


@dataclass(frozen=True)
class PortAmbiguityRecord:
    """The machine-readable refusal: what was asked, which ports could answer, and how
    a fuller sentence can name exactly one of them."""

    reason: str
    source_requirement: str
    role: str
    element_tag: str
    symbol_key: str
    selector: ParsedSelector | None
    candidates: tuple[PortCandidate, ...]
    contract_version: str = PORT_SELECTOR_CONTRACT_VERSION

    def model(self) -> dict[str, Any]:
        return {
            "code": "port_ambiguity",
            "reason": self.reason,
            "source_requirement": self.source_requirement,
            "role": self.role,
            "element_tag": self.element_tag,
            "symbol_key": self.symbol_key,
            "selector": self.selector.model() if self.selector else None,
            "candidates": [candidate.model() for candidate in self.candidates],
            "port_selector_contract_version": self.contract_version,
        }


def _absolute(port: Any, width: float, height: float) -> tuple[float, float]:
    """Frozen anchors are normalized to the intrinsic box; side is geometry in the box."""

    return (port.normalized_x * width, port.normalized_y * height)


#: Side words also speak through the port's own name: the geometric rule classifies
#: 顶部管口 (y=6 of a 100-tall box, tolerance 5.6) as interior, but a sentence saying
#: 「顶部」means that port. The predicate is geometric side OR declared side word in the
#: frozen name -- one deterministic rule, not a second geometry implementation.
_SIDE_NAME_WORDS: dict[str, tuple[str, ...]] = {
    "top": ("顶部", "塔顶", "上面", "顶上", "上端"),
    "bottom": ("底部", "塔底", "下面", "底下", "下端"),
    "left": ("左侧", "左边", "左端"),
    "right": ("右侧", "右边", "右端"),
}


def _port_satisfies(port: Any, predicate: SelectorPredicate, *, width: float, height: float) -> bool:
    if predicate.kind == "port_id":
        return port.port_id == predicate.value
    if predicate.kind == "side":
        if port_side(width, height, *_absolute(port, width, height)) == predicate.value:
            return True
        return any(word in port.name for word in _SIDE_NAME_WORDS.get(predicate.value, ()))
    if predicate.kind == "direction":
        return port.direction == predicate.value
    if predicate.kind == "semantic":
        if port.medium == predicate.value:
            return True
        # semantic tokens also speak through the port's own name (管程/回流/侧线...)
        return predicate.value in normalize_name_tokens(port.name)
    if predicate.kind == "name_token":
        return predicate.value in port.name
    return False


def candidate_hints(*, ports: tuple[Any, ...], width: float, height: float) -> tuple[PortCandidate, ...]:
    """Every compatible port as a receipt row, with the phrases that distinguish it.

    ``selectors`` lists, per candidate and in ascending order, the exact port id plus
    the Layer-1/Layer-2 phrases that match this port and no other candidate -- what a
    fuller sentence can say to name it. The machine list is complete; any truncation
    is presentation-only.
    """

    rows: list[PortCandidate] = []
    names = {port.port_id: normalize_name_tokens(port.name) for port in ports}
    for port in ports:
        distinguishing = {port.port_id}
        others = [other for other in ports if other.port_id != port.port_id]
        for table in _TOKEN_TABLES:
            for token, (kind, value) in table.items():
                if not _port_satisfies(port, SelectorPredicate(kind, value), width=width, height=height):
                    continue
                if all(
                    not _port_satisfies(other, SelectorPredicate(kind, value), width=width, height=height)
                    for other in others
                ):
                    distinguishing.add(token)
        for token in names[port.port_id]:
            if all(token not in names[other.port_id] for other in others):
                distinguishing.add(token)
        rows.append(
            PortCandidate(
                port_id=port.port_id,
                name=port.name,
                direction=port.direction,
                medium=port.medium,
                side=port_side(width, height, *_absolute(port, width, height)),
                selectors=tuple(sorted(distinguishing)),
            )
        )
    return tuple(sorted(rows, key=lambda row: row.port_id))


def resolve_with_selector(
    *,
    ports: tuple[Any, ...],
    allowed_directions: tuple[str, ...],
    selector: ParsedSelector,
    width: float,
    height: float,
) -> tuple[tuple[Any, ...], tuple[PortCandidate, ...]]:
    """Filter role-compatible ports by the selector's conjunctive predicates.

    Returns the survivors and the full candidate hints. Exactly one survivor binds;
    the caller reports zero and many -- never a default.
    """

    compatible = tuple(port for port in ports if port.direction in allowed_directions)
    surviving = tuple(
        port
        for port in compatible
        if all(
            _port_satisfies(port, predicate, width=width, height=height)
            for predicate in selector.predicates
        )
    )
    return surviving, candidate_hints(ports=compatible, width=width, height=height)


__all__ = [
    "PORT_SELECTOR_CONTRACT_VERSION",
    "REASON_MISSING_SELECTOR",
    "REASON_NO_MATCH",
    "REASON_STILL_AMBIGUOUS",
    "ParsedSelector",
    "PortAmbiguityRecord",
    "PortCandidate",
    "SelectorPredicate",
    "candidate_hints",
    "normalize_name_tokens",
    "parse_selector",
    "resolve_with_selector",
]
