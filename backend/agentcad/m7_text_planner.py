"""One sentence, one specification: code proposes the candidates, System One chooses, code writes.

The M7 path does not take a drawing instruction; it takes a :class:`DiagramSpec` -- meaning and
intent, with no coordinate anywhere. So planning is not "write me a drawing". It is a small number
of narrow choices, each between candidates that code has already built and already proved valid:

* **which catalogue symbol does a device phrase name** -- candidates are the hint-narrowed
  catalogue, so the answer cannot be a symbol the registry does not carry;
* **which two declared devices does a connection phrase join** -- candidates are the ordered pairs
  of devices the sentence has already declared, so the answer cannot be a pipe into nothing;
* **which of two same-kind devices does a bare tag refer to** -- asked only when the sentence gives
  code nothing exact to match.

Everything else is code: clause splitting, tag extraction, id allocation, system assignment,
uniqueness of engineering ids, and the assembly of the specification. Two consequences worth
naming, because both are the point of doing it this way:

* **The output is always a valid specification.** The model can only answer with a candidate that
  is already inside the question, so no hallucinated device or dangling endpoint can reach the
  layout engine -- there is no parse step because there is nothing to parse.
* **An uncertain clause is reported, not drawn.** Below :data:`DEFAULT_CONFIDENCE_FLOOR` the clause
  is skipped and named in the notes, because drawing the wrong device is worse than drawing
  nothing, and a silently dropped clause is worse than both.

The planner is upstream of the M7 chain and deliberately does not import it: it produces a
specification, and the deterministic path from that specification is somebody else's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .device_phrases import (
    MAX_CONNECTION_CANDIDATES,
    Clause,
    SymbolCandidate,
    candidate_symbols,
    extract_tags,
    normalise,
    split_clauses,
)
from .m7_diagram_spec import (
    DiagramConnection,
    DiagramEntity,
    DiagramSpec,
    DiagramSystem,
)
from .symbols import SymbolRegistry
from .typesafe import DEFAULT_CONFIDENCE_FLOOR, TypesafeClient, TypesafeError, choice_probabilities

#: The system a sentence declares when it does not name one. A drawing with no system would leave
#: every element unassigned, and the spec's own coherence rules require the system to be declared.
DEFAULT_SYSTEM_ID = "S_main"
DEFAULT_SYSTEM_NAME = "主工艺系统"

#: How an entity is named in the drawing when the sentence gives no tag. Deterministic, so the same
#: sentence twice produces the same specification twice.
UNTAGGED_TAG_TEMPLATE = "E-{index:02d}"

#: The device words that name a *kind* rather than an individual device: 「塔」 is a lookup when the
#: catalogue holds one tower symbol, 「分离塔」 is a judgment because it names which tower. The
#: comparison is whole-phrase equality, so a compound phrase is never mistaken for its own part --
#: 「缓冲罐」 is not 「罐」, and that difference is exactly the one that needs a judgment.
DEVICE_NOUNS = (
    "泵", "阀", "罐", "塔", "换热器", "过滤器", "冷凝器", "蒸发器", "容器", "鼓风机", "压缩机",
    "pump", "valve", "tank", "vessel", "column", "tower", "exchanger", "filter", "blower",
    "compressor", "fan", "drum",
)

#: The words that drop out of a clause before its device noun is read, and that must not become part
#: of a name: the verbs and connectors the sentence is made of.
#: Longest first, and the multi-word forms have to be present: 「添加一个」 replaced by 「加一个」
#: leaves a stray 「添」 glued to the device phrase, which then reaches the judgment as a word that
#: is not a device.
FILLER_WORDS = (
    "添加一个", "添加一台", "添加", "新增", "加一个", "加台", "加个",
    "放置一个", "放置", "放一个", "放台", "画一个", "画个", "建立", "新建",
    "接到", "连到", "连接到", "连接", "接入", "管线", "管道", "连线", "把", "的", "和", "与",
    "一个", "一台", "一条", "然后", "再", "请", "帮我",
    "add", "place", "draw", "create", "connect", "pipe", "line", "to", "and", "a", "an", "the",
)


class DiagramSpecPlanningError(TypesafeError):
    """The sentence could not be turned into a specification."""


@dataclass(frozen=True)
class PlannedEntity:
    """One device the sentence declares, and how code decided the parts a judgment cannot."""

    engineering_id: str
    tag: str
    phrase: str
    candidates: tuple[SymbolCandidate, ...]
    chosen_symbol_key: str = ""
    confidence: float = 1.0
    decided_by: str = "lookup"  # "lookup" | "judgment" | "uncertain"

    @property
    def resolved(self) -> bool:
        return bool(self.chosen_symbol_key)


@dataclass(frozen=True)
class PlannedConnection:
    """One connection the sentence declares, between two already-declared devices."""

    engineering_id: str
    phrase: str
    endpoints: tuple[str, str]
    candidates: tuple[tuple[str, str], ...]
    chosen: tuple[str, str] | None = None
    confidence: float = 1.0
    decided_by: str = "lookup"


@dataclass(frozen=True)
class PlannedDiagram:
    """The planning result: a specification, what it cost to decide, and what was skipped."""

    spec: DiagramSpec
    entities: tuple[PlannedEntity, ...]
    connections: tuple[PlannedConnection, ...]
    notes: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    model: str = ""
    latency_ms: float = 0.0
    question_count: int = 0
    judgment_count: int = 0
    #: The tags a clause named that no entity carries. Reported so a typo is visible rather than
    #: silently becoming a second device.
    unknown_tags: tuple[str, ...] = field(default=())


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", text).strip("_")
    return cleaned or "device"


def _entity_id(index: int, tag: str, symbol_key: str) -> str:
    """A stable engineering id. Derived from the tag when there is one, from the position if not."""

    return f"el_{_slug(tag)}" if tag else f"el_{index}_{_slug(symbol_key)}"


def _device_phrase(clause: str) -> str:
    """The clause with its verbs, connectors and tag removed: what is left is the device phrase.

    The tag is removed as a plain substring rather than on word boundaries: a Chinese device word
    and a Latin tag sit next to each other with no boundary between them (「缓冲罐V-101」), so a
    ``\\b`` anchored replacement would leave the tag inside the phrase and send it to the judgment
    as if it were part of the device's name.
    """

    text = clause
    for word in sorted(FILLER_WORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    for tag in extract_tags(clause):
        text = re.sub(re.escape(tag), " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" 　，,、。")


def _is_bare_kind(phrase: str) -> bool:
    """Whether the phrase names a device kind and nothing else, so no judgment is needed.

    「泵」 is a lookup: the hint table narrows the catalogue and the clause's own words pick the
    kind. 「燃料盐泵」 is a judgment: it names a *specific* device whose symbol the catalogue spells
    in English, and no dictionary can be trusted to hold that translation.
    """

    lowered = normalise(phrase)
    return any(word == lowered for word in DEVICE_NOUNS)


class TypesafeDiagramSpecPlanner:
    """Turns one sentence into a specification, using judgments only where a lookup cannot decide."""

    def __init__(
        self,
        symbols: SymbolRegistry,
        *,
        client_factory=TypesafeClient,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        system_id: str = DEFAULT_SYSTEM_ID,
        system_name: str = DEFAULT_SYSTEM_NAME,
    ) -> None:
        self.symbols = symbols
        self.client_factory = client_factory
        self.confidence_floor = confidence_floor
        self.system_id = system_id
        self.system_name = system_name

    # -- the code half: read the sentence, build the candidates -------------------------------- #

    def read(self, prompt: str) -> tuple[list[PlannedEntity], list[Clause], tuple[str, ...]]:
        """Every device the sentence declares, plus the connection clauses and any unknown tags.

        Done before any judgment is asked, because the connection questions need the device list:
        a connection clause can only be about devices the same sentence has already declared.
        """

        add_clauses = [clause for clause in split_clauses(prompt) if clause.kind == "add"]
        connect_clauses = [clause for clause in split_clauses(prompt) if clause.kind == "connect"]
        entities: list[PlannedEntity] = []
        known_tags: list[str] = []
        for index, clause in enumerate(add_clauses, start=1):
            tags = extract_tags(clause.text)
            tag = tags[0] if tags else UNTAGGED_TAG_TEMPLATE.format(index=index)
            phrase = _device_phrase(clause.text) or clause.text
            candidates = tuple(candidate_symbols(self.symbols, clause.text))
            symbol_key = ""
            decided_by = "judgment"
            if _is_bare_kind(phrase) and len(candidates) == 1:
                # The phrase names a kind and the catalogue holds exactly one symbol for it: that
                # is a lookup, and spending a judgment on it would only add a way to be wrong.
                symbol_key = candidates[0].key
                decided_by = "lookup"
            entities.append(
                PlannedEntity(
                    engineering_id=_entity_id(index, tag, candidates[0].key if candidates else ""),
                    tag=tag,
                    phrase=phrase,
                    candidates=candidates,
                    chosen_symbol_key=symbol_key,
                    decided_by=decided_by,
                )
            )
            known_tags.append(tag)
        # A tag named by a connection clause that no device carries is a reference to nothing. It is
        # reported rather than turned into a device: creating the missing device would be inventing
        # plant the sentence never asked for.
        declared = set(known_tags)
        mentioned = {
            tag
            for clause in connect_clauses
            for tag in extract_tags(clause.text)
        }
        unknown = tuple(sorted(mentioned - declared))
        return entities, connect_clauses, unknown

    def connection_candidates(
        self, entities: Sequence[PlannedEntity], clause: Clause
    ) -> tuple[tuple[str, str], ...]:
        """The ordered pairs this clause could join, with any tag it names pinned by code first.

        A connection has a direction, and the only thing in the sentence that says which end is
        which is the order the tags were written in -- so a clause naming two tags needs no
        judgment at all.
        """

        by_tag = {entity.tag: entity for entity in entities}
        named = [tag for tag in extract_tags(clause.text) if tag in by_tag]
        if len(named) >= 2:
            return ((by_tag[named[0]].engineering_id, by_tag[named[1]].engineering_id),)
        if len(named) == 1:
            first = by_tag[named[0]]
            return tuple(
                (first.engineering_id, other.engineering_id)
                for other in entities
                if other.engineering_id != first.engineering_id
            )[:MAX_CONNECTION_CANDIDATES]
        return tuple(
            (source.engineering_id, target.engineering_id)
            for source in entities
            for target in entities
            if source.engineering_id != target.engineering_id
        )[:MAX_CONNECTION_CANDIDATES]

    # -- the questions: one per undecided choice, all asked together --------------------------- #

    def questions(
        self, entities: Sequence[PlannedEntity], connections: Sequence[PlannedConnection]
    ) -> tuple[dict, dict]:
        #: The state the judgments read, as named fields: the phrase, the tags the sentence already
        #: declared, and the candidates, with the criterion text carrying the catalogue identity so
        #: the answer is a choice among spelled-out options rather than a free string.
        state: dict = {
            "request": {"tags": [entity.tag for entity in entities]},
        }
        questions: dict = {}
        for entity in entities:
            if entity.resolved or not entity.candidates:
                continue
            key = entity.engineering_id
            state[key] = {"phrase": entity.phrase, "tag": entity.tag}
            questions[key] = {
                "type": "choice",
                "instructions": {
                    "question": (
                        f"Which catalogue symbol should be drawn for `{key}` "
                        f"(设备短语「{entity.phrase}」, 位号 {entity.tag})?"
                    ),
                    "target": key,
                    "context": (
                        "The phrase is from a P&ID drawing request written by a Chinese-speaking "
                        "engineer. Choose the symbol whose equipment class and typical service the "
                        "phrase names. The tag is an identifier, not a hint about the kind."
                    ),
                },
                "criteria": {
                    candidate.key: (
                        f"{candidate.name or candidate.key} · category {candidate.category}"
                        + (f" · {candidate.description}" if candidate.description else "")
                    )
                    for candidate in entity.candidates
                },
            }
        label_of = {entity.engineering_id: entity.tag for entity in entities}
        for connection in connections:
            if len(connection.candidates) <= 1:
                continue
            key = connection.engineering_id
            state[key] = {"phrase": connection.phrase}
            questions[key] = {
                "type": "choice",
                "instructions": {
                    "question": (
                        f"Which two declared devices does `{key}` ('{connection.phrase}') join?"
                    ),
                    "target": key,
                    "context": (
                        "Both devices are declared by the same sentence. Each option is an ordered "
                        "pair: the first is the source, the second the target."
                    ),
                },
                "criteria": {
                    f"{label_of[source]} -> {label_of[target]}": (
                        f"connect {label_of[source]} to {label_of[target]}"
                    )
                    for source, target in connection.candidates
                },
            }
        return state, questions

    # -- the judgment, then the specification -------------------------------------------------- #

    def plan(self, prompt: str, *, typesafe_config) -> PlannedDiagram:
        """Read the sentence, ask what code cannot decide, and assemble the specification."""

        entities, connect_clauses, unknown = self.read(prompt)
        if not entities:
            raise DiagramSpecPlanningError(
                "typesafe_spec_no_device",
                f"这句话里没有认出任何设备：「{prompt.strip()}」；"
                "请用「添加一个XX泵」这样的说法。",
                status_code=422,
            )
        connections = [
            PlannedConnection(
                engineering_id=f"cn_{index}",
                phrase=clause.text,
                endpoints=("", ""),
                candidates=self.connection_candidates(entities, clause),
            )
            for index, clause in enumerate(connect_clauses, start=1)
        ]

        notes: list[str] = []
        skipped: list[str] = []
        model = ""
        latency = 0.0
        judgments = 0
        state, questions = self.questions(entities, connections)
        if questions:
            result = self.client_factory(typesafe_config).judge(state, questions)
            answers = result["answers"]
            model = str(result.get("model", ""))
            latency = float(result.get("latency_ms", 0.0))
            judgments = len(questions)
        else:
            answers = {}

        entities = [
            self._resolve_entity(entity, answers.get(entity.engineering_id), notes, skipped)
            for entity in entities
        ]
        # The label map is built after the entities are resolved, because a connection option is
        # named by the tags the sentence wrote and those are now final.
        labels = {entity.engineering_id: entity.tag for entity in entities}
        connections = [
            self._resolve_connection(
                connection, answers.get(connection.engineering_id), labels, notes, skipped
            )
            for connection in connections
        ]

        spec = self._spec(entities, connections)
        notes.insert(
            0,
            f"TypeSafe 判读：{judgments} 个判断（{len(entities)} 个设备、{len(connections)} 条连接），"
            f"代码查找决定 {sum(1 for e in entities if e.decided_by == 'lookup')} 个设备。",
        )
        if model:
            notes.append(f"model={model} latency={latency:.0f}ms")
        if unknown:
            skipped.append(f"连接短语提到的位号 {list(unknown)} 没有任何设备声明，已跳过。")
        problems = spec.problems()
        if problems:
            # Unreachable through this planner -- every value here comes from a candidate set code
            # built -- but asserted rather than assumed, because the whole claim is that the output
            # is always a valid specification.
            raise DiagramSpecPlanningError(
                "typesafe_spec_incoherent",
                "planner produced an incoherent specification: " + "; ".join(problems),
                status_code=500,
            )
        return PlannedDiagram(
            spec=spec,
            entities=tuple(entities),
            connections=tuple(connections),
            notes=tuple(notes),
            skipped=tuple(skipped),
            model=model,
            latency_ms=latency,
            question_count=len(questions),
            judgment_count=judgments,
            unknown_tags=unknown,
        )

    def _resolve_entity(
        self,
        entity: PlannedEntity,
        answer: Mapping | None,
        notes: list[str],
        skipped: list[str],
    ) -> PlannedEntity:
        if entity.resolved:
            return entity
        if not entity.candidates:
            skipped.append(f"「{entity.phrase}」在符号库里没有候选，已跳过。")
            notes.append(f"[跳过] {entity.tag}：没有候选符号。")
            return entity
        if answer is None:
            skipped.append(f"「{entity.phrase}」没有得到判断，已跳过。")
            notes.append(f"[跳过] {entity.tag}：没有判断。")
            return entity
        choice = str(answer.get("choice", "")).strip()
        confidence = float(answer.get("confidence") or 0.0)
        if confidence < self.confidence_floor:
            skipped.append(
                f"「{entity.phrase}」最高置信度 {confidence:.2f} 低于阈值 "
                f"{self.confidence_floor:.2f}（{choice or '无候选'}），已跳过。"
            )
            notes.append(f"[跳过] {entity.tag}：置信度 {confidence:.2f}。")
            return entity
        key = choice if any(row.key == choice for row in entity.candidates) else ""
        if not key:
            # The answer has to be one of the candidates code offered. It is read back as a key
            # rather than taken as free text, so anything else is a judgment artifact, not a symbol.
            probabilities = choice_probabilities(answer)
            key = max(probabilities, key=probabilities.get) if probabilities else ""
            key = key if any(row.key == key for row in entity.candidates) else ""
        if not key:
            skipped.append(f"「{entity.phrase}」的答案不在候选里（{choice!r}），已跳过。")
            notes.append(f"[跳过] {entity.tag}：答案不在候选里。")
            return entity
        notes.append(f"{entity.tag} → {key}（置信度 {confidence:.2f}）")
        return PlannedEntity(
            engineering_id=entity.engineering_id,
            tag=entity.tag,
            phrase=entity.phrase,
            candidates=entity.candidates,
            chosen_symbol_key=key,
            confidence=confidence,
            decided_by="judgment",
        )

    def _resolve_connection(
        self,
        connection: PlannedConnection,
        answer: Mapping | None,
        labels: Mapping[str, str],
        notes: list[str],
        skipped: list[str],
    ) -> PlannedConnection:
        if len(connection.candidates) == 1:
            return PlannedConnection(
                engineering_id=connection.engineering_id,
                phrase=connection.phrase,
                endpoints=connection.candidates[0],
                candidates=connection.candidates,
                chosen=connection.candidates[0],
                confidence=1.0,
                decided_by="lookup",
            )
        if answer is None or not connection.candidates:
            skipped.append(f"「{connection.phrase}」没有得到判断，已跳过。")
            return connection
        choice = str(answer.get("choice", "")).strip()
        confidence = float(answer.get("confidence") or 0.0)
        if confidence < self.confidence_floor:
            skipped.append(
                f"「{connection.phrase}」置信度 {confidence:.2f} 低于阈值，已跳过。"
            )
            return connection
        for source, target in connection.candidates:
            if choice == f"{labels.get(source, source)} -> {labels.get(target, target)}":
                notes.append(f"{connection.phrase} → {choice}（置信度 {confidence:.2f}）")
                return PlannedConnection(
                    engineering_id=connection.engineering_id,
                    phrase=connection.phrase,
                    endpoints=(source, target),
                    candidates=connection.candidates,
                    chosen=(source, target),
                    confidence=confidence,
                    decided_by="judgment",
                )
        skipped.append(f"「{connection.phrase}」的答案不在候选里（{choice!r}），已跳过。")
        return connection

    def _spec(
        self, entities: Sequence[PlannedEntity], connections: Sequence[PlannedConnection]
    ) -> DiagramSpec:
        resolved = [entity for entity in entities if entity.resolved]
        kept = {entity.engineering_id for entity in resolved}
        return DiagramSpec(
            label="自然语言生成的图纸",
            systems=[DiagramSystem(system_id=self.system_id, name=self.system_name, order=0)],
            entities=[
                DiagramEntity(
                    engineering_id=entity.engineering_id,
                    kind="equipment" if _is_equipment(entity.chosen_symbol_key, self.symbols) else "instrument",
                    system_id=self.system_id,
                    tag=entity.tag,
                    name=entity.phrase,
                    equipment_class=_category_of(entity.chosen_symbol_key, self.symbols),
                    symbol_key=entity.chosen_symbol_key,
                )
                for entity in resolved
            ],
            connections=[
                DiagramConnection(
                    engineering_id=connection.engineering_id,
                    source_engineering_id=connection.chosen[0],
                    target_engineering_id=connection.chosen[1],
                    medium="",
                    tag="",
                )
                for connection in connections
                if connection.chosen and connection.chosen[0] in kept and connection.chosen[1] in kept
            ],
        )


def _category_of(symbol_key: str, registry: SymbolRegistry) -> str:
    try:
        return registry.get(symbol_key).category
    except Exception:  # pragma: no cover - a candidate always came from the registry
        return ""


def _is_equipment(symbol_key: str, registry: SymbolRegistry) -> bool:
    """Equipment or instrument, read from the catalogue rather than guessed from the tag.

    The spec requires one of the two, and the catalogue already knows which it is -- deriving it
    from a tag prefix would be a second, weaker copy of that knowledge.
    """

    category = _category_of(symbol_key, registry).casefold()
    return not any(word in category for word in ("instrument", "transmitter", "sensor", "仪表"))


__all__ = [
    "DEFAULT_SYSTEM_ID",
    "DEFAULT_SYSTEM_NAME",
    "DEVICE_NOUNS",
    "DiagramSpecPlanningError",
    "PlannedConnection",
    "PlannedDiagram",
    "PlannedEntity",
    "TypesafeDiagramSpecPlanner",
]
