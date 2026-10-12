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
    # 冷凝器 names its own symbol: a precise hint, not a member of the broad exchanger
    # pool -- without it the phrase opens the whole catalogue and a judgment can land on
    # an unrelated symbol whose ports (in/top) can never serve 工艺入口/工艺出口.
    (("冷凝", "冷凝器", "condenser"), ("condenser",)),
    (("换热", "冷却", "加热", "exchanger", "cooler", "heater"), ("exchanger", "cooler", "heater")),
    (("流量", "flow"), ("flow",)),
    (("仪表", "变送", "transmitter", "instrument"), ("instrument", "transmitter", "sensor")),
    (("过滤", "filter", "床"), ("filter", "bed")),
)

#: Exact device phrases bypass the generic hint table. An exact phrase names one symbol
#: and only that symbol may be offered: 温度变送器 must not pull pressure_transmitter
#: into the candidate set merely because both keys contain "transmitter". A hit is
#: honoured only when exactly one exact phrase matches -- two exact phrases in one
#: clause fall back to the generic table rather than silently picking one.
EXACT_DEVICE_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("温度变送器", "temperature transmitter"), ("temperature_transmitter",)),
    # 回流罐（reflux drum）是蒸馏回流的卧式受液容器：精确词条直连 horizontal_vessel，
    # 不让通用「罐」类把它带进立式圆柱体等形似但端口语义不同的候选（DI-1-D4 F-a，
    # 基线实测「回流罐」最佳候选 cylinder_vessel 置信度 0.27 < 阈值 0.34 被跳过）。
    (("回流罐", "reflux drum", "reflux accumulator"), ("horizontal_vessel",)),
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
#: 「在 <HOST> 的 <方位> 加/安装 <量词> <仪表> <TAG>」——以方位词指明挂接位置的仪表挂接
#: 子句（DI-1-D4 F-b 最小语法扩展）。仅当所加对象是仪表（表/计/变送/indicator 词尾）时
#: 才按挂接处理；「在 X 旁边加一个罐」这类方位放置不进入本语法，保持原分类。
INSTRUMENT_ON_HOST_RE = re.compile(
    r"^在\s*[A-Za-z]{1,5}-?\d{1,5}[A-Za-z]?\s*的[^，,；;。]*?"
    r"(?:加|加装|安装|装上|设置)\s*[一两][台个只款]?"
    r"[^，,；;。]*(?:表|计|变送|transmitter|indicator|gauge|meter)"
)
CLAUSE_BREAK = re.compile(r"[，,;；。\n]+")

#: An engineering tag written in the sentence: ``V-101``, ``PT-101``, ``X-301A``. Read by code
#: because it is a pattern, not a semantic question -- a tag the user spells out is data.
TAG_PATTERN = re.compile(r"[A-Za-z]{1,5}-?\d{1,5}[A-Za-z]?")

#: 「把/将 <TAG> 换成/替换为/改成 <量词?> <新设备短语>」——替换一个已存在且明确点名的
#: 对象（DI-1-D4 F-e 最小语法）。位号必须显式写出：语法绝不推断要替换谁。
REPLACE_RE = re.compile(
    r"^(?:把|将)\s*(?P<tag>[A-Za-z]{1,5}-?\d{1,5}[A-Za-z]?)\s*"
    r"(?:换成|替换为|更换为|换成是|改成|改为)\s*(?P<rest>.+)$"
)

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
        if INSTRUMENT_ON_HOST_RE.match(text):
            # 「在 HOST 的 方位 加 仪表」: an attachment written by location. Classified as
            # an add so the attachment pipeline (host prefix -> host_engineering_id ->
            # governed tap resolution) handles it exactly like the 「给 HOST 添加」 form.
            clauses.append(Clause(text=text, kind="add"))
        elif REPLACE_RE.match(text):
            # 「把 <TAG> 换成 …」: a replacement names its target by tag -- data, not a
            # judgment. Kind "replace"; the editor resolves the new device kind among
            # candidates while the object's identity (tag, connections) is kept.
            clauses.append(Clause(text=text, kind="replace"))
        elif any(verb in lowered for verb in CONNECT_VERBS):
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


def replace_target(clause: str) -> tuple[str, str] | None:
    """(existing tag, replacement device phrase) from a replace clause, else None.

    The target tag is mandatory and read as data. The phrase is returned raw; a tag inside
    it (「把 XV-101 换成控制阀 FCV-102」) is the caller's receipt case -- v1 replaces in
    place and never renames.
    """

    match = REPLACE_RE.match(clause or "")
    if not match:
        return None
    rest = match.group("rest").strip(" 　，,、。")
    if not rest:
        return None
    return match.group("tag").upper(), rest


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
    exact_hits = [
        hints for phrases, hints in EXACT_DEVICE_HINTS if any(phrase in lowered for phrase in phrases)
    ]
    if len(exact_hits) == 1:
        return exact_hits[0]
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


#: The measure words an embedded device declaration must open with -- mandatory. A segment
#: without a measure word is not the declaration grammar and stays plain connection text.
_EMBED_MEASURE_WORDS = ("一台", "一个", "一只", "一款")


def _segment_embedded_declaration(
    segment: str, *, existing_tags: frozenset[str]
) -> tuple[str, str] | None:
    """(tag, device_phrase) when one connect-clause end is 量词+设备短语+唯一新 tag."""

    text = segment.strip().strip("，,。").strip()
    measure = next((word for word in _EMBED_MEASURE_WORDS if text.startswith(word)), None)
    if measure is None:
        return None
    rest = text[len(measure):].strip()
    tags = extract_tags(rest)
    if len(tags) != 1:
        return None
    tag = tags[0]
    if tag in existing_tags:
        return None
    if any(other in rest for other in _EMBED_MEASURE_WORDS):
        return None  # a second measure word: not the narrow grammar
    lowered = normalise(rest)
    if any(verb in lowered for verb in (*CONNECT_VERBS, *REMOVE_VERBS, *ADD_VERBS)):
        return None
    if not matched_hints(text):
        return None  # no catalogue hint: never mint a device the words do not name
    phrase = re.sub(re.escape(tag), " ", rest, flags=re.IGNORECASE)
    phrase = re.sub(r"\s+", " ", phrase).strip(" 　，,、。")
    if not phrase:
        return None
    return tag, phrase


def embedded_device_declaration(
    clause: Clause, *, existing_tags: frozenset[str]
) -> tuple[str, str] | None:
    """The new device a connect clause declares inline, or None.

    Only the narrow grammar qualifies: one end of the clause is 「量词 + 设备短语 +
    唯一新 tag」. Both ends declaring, no measure word, no tag, a tag that already exists,
    or a phrase without a catalogue hint all refuse -- the clause then flows through the
    ordinary undelivered path. The clause text is never rewritten: the connection's
    source requirement stays the user's verbatim sentence.
    """

    if clause.kind != "connect":
        return None
    text = clause.text
    verb_end = max(
        (text.find(verb) + len(verb) for verb in (*CONNECT_VERBS,) if verb in text),
        default=None,
    )
    if verb_end is None:
        return None
    target = _segment_embedded_declaration(text[verb_end:], existing_tags=existing_tags)
    source_text = text[: text.find(next(v for v in (*CONNECT_VERBS,) if v in text))]
    source = _segment_embedded_declaration(
        source_text.split("把", 1)[-1], existing_tags=existing_tags
    )
    found = [item for item in (source, target) if item is not None]
    if len(found) != 1:
        return None
    return found[0]


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
