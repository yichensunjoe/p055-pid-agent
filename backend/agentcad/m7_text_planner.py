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
from dataclasses import dataclass, field, replace

from .device_phrases import (
    ADD_VERBS,
    CONNECT_VERBS,
    MAX_CONNECTION_CANDIDATES,
    REMOVE_VERBS,
    Clause,
    SymbolCandidate,
    available_alternatives,
    candidate_symbols,
    embedded_device_declaration,
    extract_tags,
    matched_hints,
    normalise,
    split_clauses,
)
from .m7_attachment_targets import resolve_attachment_target
from .m7_diagram_spec import (
    DiagramConnection,
    DiagramEntity,
    DiagramSpec,
    DiagramSystem,
)
from .m7_synthesis_contract import Completeness
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
    #: The clause this entity was declared by, verbatim. A catalogue-gap receipt names it as the
    #: source requirement, so the record must carry what the sentence actually said.
    source_clause: str = ""
    #: The clause named a kind the catalogue does not carry (matched hint, zero rows). This is a
    #: catalogue gap, recorded so completeness can say partial -- never widened into a choice
    #: among symbols the sentence did not ask for.
    catalog_gap: bool = False
    #: Q2R3-A: the host tag from an explicit 「给 <HOST> 添加 <仪表>」. Tag form at plan time;
    #: :meth:`_spec` resolves it to the host's engineering id (instruments only -- equipment
    #: never carries a host, and a process port is never silently promoted to a tap).
    host_tag: str = ""

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
    #: M7-Q2: the raw selector phrase naming each endpoint's port (「V-101 的顶部管口」).
    #: Travels beside the spec into the binding step; it is an input to resolution, never
    #: part of the specification or any digest.
    source_port_selector: str = ""
    target_port_selector: str = ""


@dataclass(frozen=True)
class PlannedDiagram:
    """The planning result: a specification, what it cost to decide, and what was skipped.

    ``completeness`` is the same three-value statement the synthesis contract uses: the
    specification may be *coherent* while still being *partial* -- a skipped device, a dropped
    connection, an unknown tag, an unrecognised clause or a catalogue gap all leave the spec
    drawable yet incomplete. Coherence is about structure; completeness is about whether the
    drawing is the whole sentence. Every input clause lands in exactly one of two places: it is
    delivered (an entity or connection in the spec) or it is receipted in ``undelivered`` --
    a clause may not silently disappear between reading and planning.
    """

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
    #: "complete" | "partial" | "empty": whether the spec is the whole sentence.
    completeness: Completeness = "complete"
    #: One receipt per input clause that nothing in the spec honours, in sentence order.
    undelivered: tuple[str, ...] = ()
    #: The machine-visible catalogue-gap records (schema = the synthesis contract's
    #: CATALOG_GAP_REQUIRED_FIELDS): what was asked, under which tag, in which clause, and
    #: what the catalogue does carry. ``available_alternatives`` is reporting only; it never
    #: flows back into a judgment's candidates.
    catalog_gaps: tuple[dict, ...] = ()
    #: Q2R3-B1: machine-readable attachment resolutions that did not resolve to a
    #: governed tap port (``instrument_attachment_ambiguity``). The prose twin rides
    #: ``undelivered``; the record stays testable and future-surface-ready without a
    #: second state machine.
    attachment_gaps: tuple[dict, ...] = ()


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", text).strip("_")
    return cleaned or "device"


def _entity_id(index: int, tag: str, symbol_key: str) -> str:
    """A stable engineering id. Derived from the tag when there is one, from the position if not."""

    return f"el_{_slug(tag)}" if tag else f"el_{index}_{_slug(symbol_key)}"


_HOST_PREFIX = re.compile(r"给\s*(?P<tag>[A-Za-z]{1,5}-?\d{1,5}[A-Za-z]?)")


def _host_tag_of(clause_text: str) -> str:
    """The host tag an explicit 「给 <HOST> 添加 …」 names, upper-cased, or "".

    The tag is data read by a pattern, not a judgment; anything the sentence does not
    spell out stays empty, and an empty host means no attachment is claimed.
    """

    match = _HOST_PREFIX.search(clause_text or "")
    return match.group("tag").upper() if match else ""


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


_PORT_SELECTOR_BREAK = ("接到", "连到", "连接到", "连接", "接入", "和", "与", "再", "，", ",", "；")


def extract_port_selectors(clause: str) -> dict[str, str]:
    """「TAG的PHRASE」 per tag: the raw port-selector phrase the sentence wrote for it.

    The phrase runs from the possessive after the tag to the next tag, a connect verb or
    the clause end. Pure string work, like everything else the sentence-reading half does.
    """

    selectors: dict[str, str] = {}
    for tag in extract_tags(clause):
        match = re.search(re.escape(tag) + r"\s*[的之]\s*(.+)$", clause)
        if not match:
            continue
        phrase = match.group(1)
        for other in extract_tags(clause):
            if other != tag:
                index = phrase.find(other)
                if index > 0:
                    phrase = phrase[:index]
        for breaker in _PORT_SELECTOR_BREAK:
            index = phrase.find(breaker)
            if index > 0:
                phrase = phrase[:index]
        phrase = phrase.strip(" 的之，,；;。")
        if phrase:
            selectors[tag] = phrase
    return selectors


def attach_port_selectors(
    connections: Sequence[PlannedConnection], labels: Mapping[str, str]
) -> list[PlannedConnection]:
    """Resolve each connection's selector phrases to its two endpoints, by the tags the
    sentence wrote. Endpoints the phrase does not name carry no selector."""

    attached: list[PlannedConnection] = []
    for connection in connections:
        if connection.chosen is None:
            attached.append(connection)
            continue
        by_tag = extract_port_selectors(connection.phrase)
        source_tag = labels.get(connection.chosen[0], "")
        target_tag = labels.get(connection.chosen[1], "")
        attached.append(
            replace(
                connection,
                source_port_selector=by_tag.get(source_tag, ""),
                target_port_selector=by_tag.get(target_tag, ""),
            )
        )
    return attached


#: The measure words a device-add enumeration counts devices with. Anything the sentence does not
#: phrase as one of these is not part of the enumeration grammar and must not be split.
_ENUM_MEASURE_WORDS = ("一台", "一个", "一只", "一款", "一台套")

#: A system declaration reads as 「…系统」 with no tag: shape-tested, not judged. Such a clause
#: names no device the catalogue could carry, so it must never become phantom equipment.
_SYSTEM_DECLARATION = re.compile(r"^[\u4e00-\u9fff]{1,8}系统$")


def _system_declaration_clause(clause: Clause) -> bool:
    """Whether an add clause declares a system rather than a device.

    Fail-closed gate: system declarations are receipted (and the sentence stays partial)
    until the planner actually supports them. The alternative -- letting the clause through
    the device path -- minted an untagged phantom E-01 and still claimed the sentence was
    complete, which is exactly the measurement-integrity failure this gate exists to close.
    """

    if clause.kind != "add" or extract_tags(clause.text):
        return False
    return bool(_SYSTEM_DECLARATION.fullmatch(_device_phrase(clause.text)))


def _expand_device_add_enumeration(clause: Clause) -> list[tuple[Clause, str | None]]:
    """Split one add clause's device enumeration into ordered per-device clauses (G2).

    Only the device-add enumeration syntax is split:
    「给 HOST 添加一台 X T1、一台 Y T2 和一台 Z T3」 becomes one add clause per item, each
    carrying its own tag, in written order, sharing the same host prefix -- so every device
    enters the clause ledger on its own instead of one merged phrase failing lookup for all
    of them. The returned tag override is the item's own tag: the host prefix may name the
    attach target (e.g. V-101), which must not win tag extraction over the device the item
    declares. Guards keep the split narrow: every item must open with a measure word and
    name exactly one tag, and no item may contain a connect/remove verb. Anything else
    returns the clause untouched -- no generic comma splitting, and connection clauses
    never enter here.
    """

    if clause.kind != "add" or "、" not in clause.text:
        return [(clause, None)]
    verb_end = next(
        (clause.text.find(verb) + len(verb) for verb in ADD_VERBS if verb in clause.text),
        None,
    )
    if verb_end is None:
        return [(clause, None)]
    host = clause.text[:verb_end]
    rest = clause.text[verb_end:]
    # 、 separates items; 和 separates the last item only when it opens with a measure word,
    # so a device phrase that happens to contain 和 is not torn apart.
    pieces = [
        piece
        for chunk in rest.split("、")
        for piece in re.split(r"和(?=一(?:台|个|只|款))", chunk)
    ]
    items: list[tuple[str, str]] = []
    for piece in pieces:
        item = piece.strip().lstrip("和").strip()
        if not item.startswith(_ENUM_MEASURE_WORDS):
            return [(clause, None)]
        lowered = normalise(item)
        if any(verb in lowered for verb in (*CONNECT_VERBS, *REMOVE_VERBS)):
            return [(clause, None)]
        item_tags = extract_tags(item)
        if len(item_tags) != 1:
            return [(clause, None)]
        items.append((item, item_tags[0]))
    if len(items) < 2:
        return [(clause, None)]
    return [(Clause(text=f"{host}{item}", kind="add"), tag) for item, tag in items]


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
        self._last_system_declarations: list[Clause] = []

    @property
    def last_system_declarations(self) -> tuple[Clause, ...]:
        """System declarations separated by the most recent :meth:`read`.

        Recognized but unsupported: :meth:`plan` receipts them, and the device path never
        sees them -- a system declaration must not mint phantom equipment.
        """

        return tuple(self._last_system_declarations)

    # -- the code half: read the sentence, build the candidates -------------------------------- #

    def read(
        self, prompt: str, *, existing_tags: tuple[str, ...] = ()
    ) -> tuple[list[PlannedEntity], list[Clause], tuple[str, ...], list[Clause]]:
        """Every device the sentence declares, the connection clauses, and what code could not read.

        ``unknown_clauses`` is returned rather than dropped: a clause the verb table cannot
        classify is a receipt the completeness account must carry, not text that silently
        vanishes between reading and planning. System declarations (add clauses that declare
        a system rather than a device) are recognized here, kept out of the device path so
        they can never become phantom equipment, and exposed through
        :attr:`last_system_declarations` for :meth:`plan` to receipt. ``existing_tags``
        carries tags the caller already knows (an edit's base spec): an inline device
        declaration colliding with one is refused a second entity.
        """

        expanded_adds = [
            expanded
            for clause in split_clauses(prompt)
            if clause.kind == "add"
            for expanded in _expand_device_add_enumeration(clause)
        ]
        device_adds = [
            (clause, tag_override)
            for clause, tag_override in expanded_adds
            if not _system_declaration_clause(clause)
        ]
        self._last_system_declarations = [
            clause
            for clause, _tag_override in expanded_adds
            if _system_declaration_clause(clause)
        ]
        add_clauses = [clause for clause, _tag_override in device_adds]
        tag_overrides = {
            id(clause): tag_override
            for clause, tag_override in device_adds
            if tag_override is not None
        }
        connect_clauses = [clause for clause in split_clauses(prompt) if clause.kind == "connect"]
        unknown_clauses = [clause for clause in split_clauses(prompt) if clause.kind == "unknown"]
        entities: list[PlannedEntity] = []
        known_tags: list[str] = []
        for index, clause in enumerate(add_clauses, start=1):
            tags = extract_tags(clause.text)
            host_tag = _host_tag_of(clause.text)
            if host_tag and len(tags) >= 2:
                # 「给 <HOST> 添加 <设备> <TAG>」: the first tag names the host, the last
                # names the device. Reading the host as the device would mint a wrong tag
                # and corrupt the attachment relation built from it.
                tag = tag_overrides.get(id(clause)) or tags[-1]
            else:
                tag = tag_overrides.get(id(clause)) or (tags[0] if tags else UNTAGGED_TAG_TEMPLATE.format(index=index))
            phrase = _device_phrase(clause.text) or clause.text
            candidates = tuple(candidate_symbols(self.symbols, clause.text))
            catalog_gap = bool(matched_hints(clause.text)) and not candidates
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
                    source_clause=clause.text,
                    catalog_gap=catalog_gap,
                    host_tag=host_tag,
                )
            )
            known_tags.append(tag)
        # Q2R2: a connect clause may declare its target device inline (「接到一个缓冲罐
        # V-102」). The declaration joins the device path with the connect clause's own text
        # as its source requirement -- the ledger records what the user actually wrote, not
        # a synthetic add sentence. Refusals (no measure word, no/colliding tag, no hint)
        # leave the clause on the ordinary undelivered path.
        known = set(existing_tags) | set(known_tags)
        for clause in connect_clauses:
            declared_inline = embedded_device_declaration(
                clause, existing_tags=frozenset(known)
            )
            if declared_inline is None:
                continue
            tag, phrase = declared_inline
            candidates = tuple(candidate_symbols(self.symbols, phrase))
            catalog_gap = bool(matched_hints(phrase)) and not candidates
            symbol_key = ""
            decided_by = "judgment"
            if _is_bare_kind(phrase) and len(candidates) == 1:
                symbol_key = candidates[0].key
                decided_by = "lookup"
            entities.append(
                PlannedEntity(
                    engineering_id=_entity_id(len(entities) + 1, tag, candidates[0].key if candidates else ""),
                    tag=tag,
                    phrase=phrase,
                    candidates=candidates,
                    chosen_symbol_key=symbol_key,
                    decided_by=decided_by,
                    source_clause=clause.text,
                    catalog_gap=catalog_gap,
                )
            )
            known.add(tag)
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
        return entities, connect_clauses, unknown, unknown_clauses

    def connection_candidates(
        self, entities: Sequence[PlannedEntity], clause: Clause
    ) -> tuple[tuple[str, str], ...]:
        """The ordered pairs this clause could join, with any tag it names pinned by code first.

        A connection has a direction, and the only thing in the sentence that says which end is
        which is the order the tags were written in -- so a clause naming two tags needs no
        judgment at all. But a tag the sentence *wrote* that no device declares is a named
        requirement, not a wildcard: pairing the known end with some other declared device
        would substitute a connection the user never asked for, so the candidate set is empty
        and the clause is receipted instead.
        """

        by_tag = {entity.tag: entity for entity in entities}
        named = [tag for tag in extract_tags(clause.text) if tag in by_tag]
        mentioned = extract_tags(clause.text)
        if any(tag not in by_tag for tag in mentioned):
            return ()
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
        self,
        entities: Sequence[PlannedEntity],
        connections: Sequence[PlannedConnection],
        *,
        label_entities: Sequence[PlannedEntity] | None = None,
    ) -> tuple[dict, dict]:
        #: The state the judgments read, as named fields: the phrase, the tags the sentence already
        #: declared, and the candidates, with the criterion text carrying the catalogue identity so
        #: the answer is a choice among spelled-out options rather than a free string.
        #: ``label_entities`` resolves connection-option labels without widening the judgment
        #: surface: an edit's connections may join base-spec devices that are not part of this
        #: sentence's judgments, and those still need a tag for the criterion text.
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
        label_of = {
            entity.engineering_id: entity.tag
            for entity in (label_entities if label_entities is not None else entities)
        }
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

        entities, connect_clauses, unknown, unknown_clauses = self.read(prompt)
        system_declarations = list(self._last_system_declarations)
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
        connections = attach_port_selectors(connections, labels)

        spec = self._spec(entities, connections)
        # Clause ledger: every declared device the spec does not carry is receipted, with the
        # catalogue gap named as its own reason. A gap is never widened into a judgment -- the
        # model may not pick a look-alike the sentence did not ask for.
        undelivered: list[str] = [f"无法理解的子句：「{clause.text}」" for clause in unknown_clauses]
        # Q2R3-A: an instrument whose named host is not declared keeps its own device but
        # loses the attachment, receipted -- never guessed onto whatever device is near.
        host_labels = {entity.tag: entity.engineering_id for entity in entities}
        for entity in entities:
            if (
                entity.host_tag
                and entity.host_tag not in host_labels
                and not _is_equipment(entity.chosen_symbol_key, self.symbols)
            ):
                undelivered.append(
                    f"「{entity.source_clause or entity.phrase}」的宿主 {entity.host_tag} "
                    "未声明，仪表挂接未建立。"
                )
        # System declarations are recognized but unsupported: receipted, never drawn, and the
        # sentence stays partial. The receipt names the unsupported feature so the gap is
        # attributable instead of being silently minted as phantom equipment.
        for clause in system_declarations:
            skipped.append(
                f"「{clause.text}」：系统声明暂不支持，已记录、未生成设备。"
            )
        delivered_entities = {entity.engineering_id for entity in spec.entities}
        catalog_gaps: list[dict] = []
        for entity in entities:
            if entity.engineering_id in delivered_entities:
                continue
            if entity.catalog_gap:
                undelivered.append(f"「{entity.phrase}」（位号 {entity.tag}）：目录里没有这类设备的符号。")
                # The machine-visible record the synthesis contract freezes: what was asked,
                # under which tag, in which clause, and what the catalogue does carry. The
                # alternatives are for a human to read into a replan; they never flow back into
                # a judgment's candidate set.
                catalog_gaps.append(
                    {
                        "requested_type": entity.phrase,
                        "requested_tag": entity.tag,
                        "source_requirement": entity.source_clause,
                        "available_alternatives": [
                            {
                                "key": row.key,
                                "name": row.name,
                                "category": row.category,
                            }
                            for row in available_alternatives(self.symbols)
                        ],
                    }
                )
            else:
                undelivered.append(f"「{entity.phrase}」（位号 {entity.tag}）没有被兑现。")
        delivered_connections = {connection.engineering_id for connection in spec.connections}
        for connection in connections:
            if connection.engineering_id not in delivered_connections:
                undelivered.append(f"「{connection.phrase}」没有被兑现。")

        attachment_gaps, attachment_receipts = self._attachment_ledger(spec)
        undelivered.extend(attachment_receipts)
        resolved_count = len(spec.entities)
        if resolved_count == 0:
            completeness: Completeness = "empty"
        elif skipped or unknown or undelivered:
            completeness = "partial"
        else:
            completeness = "complete"
        notes.insert(
            0,
            f"TypeSafe 判读：{judgments} 个判断（{len(entities)} 个设备、{len(connections)} 条连接），"
            f"代码查找决定 {sum(1 for e in entities if e.decided_by == 'lookup')} 个设备。"
            f"完整度：{completeness}。",
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
            completeness=completeness,
            undelivered=tuple(undelivered),
            catalog_gaps=tuple(catalog_gaps),
            attachment_gaps=attachment_gaps,
        )

    def _attachment_ledger(
        self, spec: DiagramSpec
    ) -> tuple[tuple[dict, ...], list[str]]:
        """Q2R3-B1: run the governed-tap resolver over every attached instrument.

        A host without a governed tap port is the honest terminal state under the
        current catalogue: the instrument stays delivered, the attachment becomes a
        machine-readable ambiguity record plus its prose receipt, and nothing guesses
        a process port. Resolved instruments leave no trace here (B2's lane).
        """

        by_id = {entity.engineering_id: entity for entity in spec.entities}
        records: list[dict] = []
        receipts: list[str] = []
        for entity in spec.entities:
            if entity.kind != "instrument" or not entity.host_engineering_id:
                continue
            host = by_id.get(entity.host_engineering_id)
            if host is None:
                continue
            host_ports: tuple[str, ...] = ()
            try:
                host_ports = tuple(
                    port.id for port in self.symbols.get(host.symbol_key).ports
                )
            except KeyError:
                host_ports = ()
            resolution = resolve_attachment_target(
                instrument_tag=entity.tag,
                instrument_symbol_key=entity.symbol_key,
                host_tag=host.tag,
                host_symbol_key=host.symbol_key,
                host_port_ids=host_ports,
            )
            if resolution.resolved:
                continue
            records.append(resolution.receipt())
            reason_text = {
                "no_governed_tap_port": f"{host.symbol_key} 没有任何受治理的仪表取压口",
                "unsupported_instrument_type": "该仪表类型没有挂接选型规则",
                "no_matching_governed_tap_port": f"{host.symbol_key} 没有该仪表类型对应的受治理取压口",
                "multiple_governed_tap_ports": f"{host.symbol_key} 有多个可接受的受治理取压口，无法唯一确定",
            }.get(resolution.reason, resolution.reason)
            receipts.append(
                f"「{entity.tag}」挂接于 {host.tag}：{resolution.reason}"
                f"（{reason_text}），挂接未建立。"
            )
        return tuple(records), receipts

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
            source_clause=entity.source_clause,
            catalog_gap=entity.catalog_gap,
            host_tag=entity.host_tag,
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
        labels = {entity.tag: entity.engineering_id for entity in resolved}
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
                    host_engineering_id=(
                        labels.get(entity.host_tag)
                        if entity.host_tag
                        and not _is_equipment(entity.chosen_symbol_key, self.symbols)
                        else None
                    ),
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
