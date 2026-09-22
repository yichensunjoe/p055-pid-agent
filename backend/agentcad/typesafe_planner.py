"""Drawing with TypeSafe: code proposes the candidates, System One chooses, code executes.

A drawing sentence does not need generated text. It needs a handful of narrow choices -- which
catalogue symbol does 「燃料盐泵」 name, which two placed devices does 「把缓冲罐接到分离塔」 connect,
does the clause ask for a flow direction -- and each of those is a judgment. This planner therefore
inverts the usual agent shape:

1. **code** splits the sentence into clauses, resolves the parts that are exact (a label that is
   already on the canvas, a port that is free), and builds the candidate set;
2. **TypeSafe** answers the parts that are semantics: which candidate the phrase means, and how sure
   it is. Anything below :data:`DEFAULT_CONFIDENCE_FLOOR` is *reported as uncertain and skipped* --
   drawing the wrong device is worse than drawing nothing;
3. **code** turns the chosen candidates into a :class:`SemanticTransaction`, which then goes through
   the same compiler, assessment and apply path every other plan uses.

The output is a plain ``SemanticAgentPlan``, so the web agent's preview, review and apply flow works
unchanged: TypeSafe changes *how the plan is chosen*, not what the system accepts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .agent_semantic_models import (
    AddElementOperation,
    ConnectPortsOperation,
    SemanticAgentPlan,
    SemanticTransaction,
)
from .models import ConnectorElement, Point, SymbolElement
from .service import DocumentService
from .symbols import SymbolRegistry
from .typesafe import (
    DEFAULT_CONFIDENCE_FLOOR,
    TypesafeClient,
    TypesafeError,
    choice_probabilities,
)

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

ADD_VERBS = ("添加", "新增", "加一个", "加个", "放置", "放一个", "画一个", "画个", "建立", "新建",
             "add", "place", "draw", "create")
CONNECT_VERBS = ("接到", "连到", "连接到", "连接", "接入", "接到", "管线", "管道", "连线",
                 "connect", "pipe", "line to")
CLAUSE_BREAK = re.compile(r"[，,;；。\n]+")

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


@dataclass(frozen=True)
class ConnectionCandidate:
    source_element_id: str
    source_label: str
    source_port_id: str
    target_element_id: str
    target_label: str
    target_port_id: str


def _normalise(text: str) -> str:
    return text.casefold().strip()


def split_clauses(prompt: str) -> list[Clause]:
    """Split the sentence and classify each clause by verb -- a string test, not a model call."""

    clauses: list[Clause] = []
    for raw in CLAUSE_BREAK.split(prompt or ""):
        text = raw.strip()
        if not text:
            continue
        lowered = _normalise(text)
        if any(verb in lowered for verb in CONNECT_VERBS):
            clauses.append(Clause(text=text, kind="connect"))
        elif any(verb in lowered for verb in ADD_VERBS):
            clauses.append(Clause(text=text, kind="add"))
        else:
            clauses.append(Clause(text=text, kind="unknown"))
    return clauses


def candidate_symbols(
    registry: SymbolRegistry, clause: str, *, limit: int = MAX_CANDIDATES
) -> list[SymbolCandidate]:
    """The symbols the clause could mean, narrowed by the hint table and then by name."""

    lowered = _normalise(clause)
    wanted: list[str] = []
    for phrases, hints in SYMBOL_HINTS:
        if any(phrase in lowered for phrase in phrases):
            wanted.extend(hints)
    rows: list[SymbolCandidate] = []
    for definition in registry.list():
        haystack = _normalise(f"{definition.key} {definition.name} {definition.category}")
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
    if wanted and not rows:
        # The hint matched a phrase but no symbol carries it: the judgement would have nothing to
        # choose between, so the whole catalogue is offered instead of an empty question.
        return [
            SymbolCandidate(
                key=definition.key,
                name=definition.name,
                category=definition.category,
                description=definition.description,
            )
            for definition in registry.list()[:limit]
        ]
    return rows[:limit]


def _placed_symbols(service: DocumentService, document_id: str) -> list[SymbolElement]:
    document = service.get_document(document_id)
    return [element for element in document.elements if isinstance(element, SymbolElement)]


def _used_ports(service: DocumentService, document_id: str) -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    for element in service.get_document(document_id).elements:
        if not isinstance(element, ConnectorElement):
            continue
        for endpoint in (element.source, element.target):
            if endpoint is not None and endpoint.element_id and endpoint.port_id:
                used.add((endpoint.element_id, endpoint.port_id))
    return used


def free_port(registry: SymbolRegistry, element: SymbolElement, used: set[tuple[str, str]]) -> str | None:
    """A port on this element that no connector uses yet, or ``None`` when there is none."""

    try:
        definition = registry.get(element.symbol_key)
    except Exception:  # pragma: no cover - a document referring to a symbol the catalogue lost
        return None
    for port in definition.ports:
        if (element.id, port.id) not in used:
            return port.id
    return None


def _free_position(service: DocumentService, document_id: str, index: int) -> Point:
    """Place new equipment to the right of what is drawn, one row per new element.

    Code decides geometry, not the model: a judgment has no idea what the canvas already holds, and
    a plan that overlaps existing equipment fails the quality gates for a reason the model cannot
    see.
    """

    drawn = [
        element
        for element in service.get_document(document_id).elements
        if isinstance(element, SymbolElement)
    ]
    if not drawn:
        return Point(x=240.0, y=160.0 + index * 120.0)
    right = max(element.position.x + element.width / 2 for element in drawn)
    top = max(element.position.y + element.height / 2 for element in drawn)
    return Point(x=right + 200.0, y=top - index * 120.0)


def _symbol_element(
    registry: SymbolRegistry, candidate: SymbolCandidate, element_id: str, position: Point, label: str
) -> SymbolElement:
    definition = registry.get(candidate.key)
    return SymbolElement(
        id=element_id,
        symbol_key=definition.key,
        position=position,
        width=definition.width,
        height=definition.height,
        label=label,
        properties={"tag": label} if label else {},
    )


def _label_of(element: SymbolElement) -> str:
    tag = element.properties.get("tag") if isinstance(element.properties, Mapping) else None
    return str(tag or element.label or "").strip()


def connection_candidates(
    service: DocumentService,
    document_id: str,
    registry: SymbolRegistry,
    clause: str,
    *,
    limit: int = MAX_CONNECTION_CANDIDATES,
) -> list[ConnectionCandidate]:
    """Every pair of drawn devices that could be the two ends, with a free port on each.

    Labels that appear verbatim in the clause are pinned first by *code*: when the user writes a tag
    the drawing already carries, that is a lookup, and spending a judgment on it would only add a
    way to get it wrong.
    """

    used = _used_ports(service, document_id)
    placed = _placed_symbols(service, document_id)
    named = [element for element in placed if _label_of(element) and _label_of(element) in clause]
    if len(named) >= 2:
        ordered = _ordered_by_appearance(named, clause)
        pairs = [(ordered[0], ordered[1])]
    elif len(named) == 1 and len(placed) >= 2:
        pairs = [(named[0], other) for other in placed if other.id != named[0].id]
    else:
        pairs = [(first, second) for first in placed for second in placed if first.id != second.id]
    candidates: list[ConnectionCandidate] = []
    for source, target in pairs:
        source_port = free_port(registry, source, used)
        target_port = free_port(registry, target, used)
        if source_port is None or target_port is None:
            continue
        candidates.append(
            ConnectionCandidate(
                source_element_id=source.id,
                source_label=_label_of(source),
                source_port_id=source_port,
                target_element_id=target.id,
                target_label=_label_of(target),
                target_port_id=target_port,
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def _ordered_by_appearance(elements: Sequence[SymbolElement], clause: str) -> list[SymbolElement]:
    return sorted(elements, key=lambda element: clause.find(_label_of(element)))


def _choice_criteria(candidates: Sequence[SymbolCandidate]) -> dict[str, str]:
    return {
        candidate.name or candidate.key: (
            f"{candidate.category} symbol `{candidate.key}`"
            + (f": {candidate.description}" if candidate.description else "")
        )
        for candidate in candidates
    }


def _connection_criteria(candidates: Sequence[ConnectionCandidate]) -> dict[str, str]:
    return {
        f"{row.source_label or row.source_element_id} -> {row.target_label or row.target_element_id}": (
            f"connect `{row.source_label or row.source_element_id}` "
            f"(its port {row.source_port_id}) to `{row.target_label or row.target_element_id}` "
            f"(its port {row.target_port_id})"
        )
        for row in candidates
    }


class TypesafeSemanticPlanner:
    """Plans a drawing by judgment. Same contract as the model planner, different kind of decision."""

    def __init__(
        self,
        service: DocumentService,
        symbols: SymbolRegistry,
        *,
        client_factory=TypesafeClient,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    ) -> None:
        self.service = service
        self.symbols = symbols
        self.client_factory = client_factory
        self.confidence_floor = confidence_floor

    def plan(self, document_id: str, request: Any) -> SemanticAgentPlan:
        """Build the plan, or raise :class:`TypesafeError` when a judgment is missing or uncertain."""

        client = self.client_factory(request.typesafe_config)
        prompt = getattr(request, "prompt", "") or ""
        clauses = split_clauses(prompt)
        if not clauses:
            raise TypesafeError(
                "typesafe_prompt_empty",
                "a drawing request needs at least one clause",
                status_code=400,
            )
        additions = [clause for clause in clauses if clause.kind == "add"]
        connections = [clause for clause in clauses if clause.kind == "connect"]
        state, questions, plans = self._build_questions(document_id, additions, connections)
        notes: list[str] = [
            f"TypeSafe 判读：{len(questions)} 个判断，候选 {len(state.get('symbol_candidates', []))}"
            f" 个符号 / {len(state.get('connection_candidates', []))} 个连接组合。"
        ]
        if questions:
            result = client.judge(state, questions)
            answers = result["answers"]
            notes.append(f"model={result['model']} latency={result['latency_ms']}ms")
        else:
            answers = {}
        operations = self._operations_from_answers(document_id, plans, answers, notes)
        if not operations:
            raise TypesafeError(
                "typesafe_no_operation_selected",
                "TypeSafe could not select any confident operation for this request; "
                + " ".join(notes),
                status_code=422,
            )
        return SemanticAgentPlan(
            explanation=" ".join(notes),
            transaction=SemanticTransaction(
                operations=operations,
                expected_revision=getattr(request, "expected_revision", None),
                label="TypeSafe 规划的图纸编辑",
            ),
        )

    def _build_questions(
        self,
        document_id: str,
        additions: Sequence[Clause],
        connections: Sequence[Clause],
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        state: dict[str, Any] = {"request": {"clauses": [c.text for c in additions + connections]}}
        questions: dict[str, Any] = {}
        plans: list[dict[str, Any]] = []
        for index, clause in enumerate(additions):
            candidates = candidate_symbols(self.symbols, clause.text)
            plans.append({"kind": "add", "clause": clause.text, "question": f"add_{index}", "candidates": candidates})
            if not candidates:
                continue
            state["symbol_candidates"] = [
                {"key": row.key, "name": row.name, "category": row.category}
                for row in candidates
            ]
            state[f"add_{index}"] = {"phrase": clause.text}
            questions[f"add_{index}"] = {
                "type": "choice",
                "instructions": {
                    "question": (
                        f"Which catalogue symbol should be drawn for `add_{index}` "
                        f"('{clause.text}')?"
                    ),
                    "target": f"add_{index}",
                    "context": (
                        "The phrase comes from a P&ID drawing request written by an engineer. "
                        "Choose the symbol the phrase names; if none of them fits, choose the "
                        "closest by category."
                    ),
                },
                "criteria": _choice_criteria(candidates),
            }
        for index, clause in enumerate(connections):
            candidates = connection_candidates(self.service, document_id, self.symbols, clause.text)
            plans.append({"kind": "connect", "clause": clause.text, "question": f"connect_{index}", "candidates": candidates})
            if not candidates:
                continue
            state["connection_candidates"] = [
                {
                    "option": f"{row.source_label or row.source_element_id} -> "
                    f"{row.target_label or row.target_element_id}",
                    "source": row.source_label or row.source_element_id,
                    "target": row.target_label or row.target_element_id,
                }
                for row in candidates
            ]
            state[f"connect_{index}"] = {"phrase": clause.text}
            questions[f"connect_{index}"] = {
                "type": "choice",
                "instructions": {
                    "question": (
                        f"Which connection does `connect_{index}` ('{clause.text}') describe?"
                    ),
                    "target": f"connect_{index}",
                    "context": (
                        "Both devices already exist on the drawing. The labels in the option names "
                        "are the tags drawn on the canvas."
                    ),
                },
                "criteria": _connection_criteria(candidates),
            }
        return state, questions, plans

    def _operations_from_answers(
        self,
        document_id: str,
        plans: Sequence[Mapping[str, Any]],
        answers: Mapping[str, Any],
        notes: list[str],
    ) -> list[Any]:
        operations: list[Any] = []
        added = 0
        for plan in plans:
            question = str(plan["question"])
            answer = answers.get(question)
            candidates = list(plan["candidates"])
            if answer is None or not candidates:
                notes.append(f"[跳过] {plan['clause']}：没有可用的判断。")
                continue
            choice = str(answer.get("choice", "")).strip()
            confidence = float(answer.get("confidence") or 0.0)
            if confidence < self.confidence_floor:
                notes.append(
                    f"[跳过] {plan['clause']}：最高置信度 {confidence:.2f} 低于阈值 "
                    f"{self.confidence_floor:.2f}（{choice or '无候选'}）。"
                )
                continue
            if plan["kind"] == "add":
                selected = _select_symbol(candidates, choice)
                if selected is None:
                    notes.append(f"[跳过] {plan['clause']}：判断给出的 {choice!r} 不在候选里。")
                    continue
                element_id = f"sym_typesafe_{document_id[:4]}{len(operations):02d}{added:02d}"
                element = _symbol_element(
                    self.symbols,
                    selected,
                    element_id,
                    _free_position(self.service, document_id, added),
                    label=_label_from_clause(plan["clause"]),
                )
                operations.append(AddElementOperation(element=element))
                added += 1
                notes.append(
                    f"[添加] {plan['clause']} -> {selected.key}（置信度 {confidence:.2f}）"
                )
            else:
                selected_connection = _select_connection(candidates, choice)
                if selected_connection is None:
                    notes.append(f"[跳过] {plan['clause']}：判断给出的 {choice!r} 不在候选里。")
                    continue
                operations.append(
                    ConnectPortsOperation(
                        source_element_id=selected_connection.source_element_id,
                        source_port_id=selected_connection.source_port_id,
                        target_element_id=selected_connection.target_element_id,
                        target_port_id=selected_connection.target_port_id,
                        process_tag=_process_tag_from_clause(plan["clause"]),
                    )
                )
                notes.append(
                    f"[连接] {plan['clause']} -> "
                    f"{selected_connection.source_label or selected_connection.source_element_id}"
                    f" → {selected_connection.target_label or selected_connection.target_element_id}"
                    f"（置信度 {confidence:.2f}）"
                )
        return operations


def _select_symbol(candidates: Sequence[SymbolCandidate], choice: str) -> SymbolCandidate | None:
    lowered = _normalise(choice)
    for candidate in candidates:
        if lowered in {_normalise(candidate.key), _normalise(candidate.name)}:
            return candidate
    return None


def _select_connection(
    candidates: Sequence[ConnectionCandidate], choice: str
) -> ConnectionCandidate | None:
    for row in candidates:
        if choice == f"{row.source_label or row.source_element_id} -> {row.target_label or row.target_element_id}":
            return row
    return None


#: A tag the engineer wrote: a latin token that carries a number (``TAG-P101``, ``P101``, ``FCV-201``).
#: Deliberately not a model output -- a label the drawing never had is worse than no label.
_TAG = re.compile(r"[A-Za-z][\w-]*\d[\w-]*")
_DIAMETER = re.compile(r"(DN|dn)\s?\d{1,4}")


def _label_from_clause(clause: str) -> str:
    """A tag the user wrote verbatim, or nothing. Never a model-invented label."""

    match = _TAG.search(clause or "")
    return match.group(0) if match else ""


def _process_tag_from_clause(clause: str) -> str:
    match = _DIAMETER.search(clause or "")
    return match.group(0) if match else ""


def confidence_of(answer: Mapping[str, Any]) -> float:
    """The judgment's own number, whatever primitive produced it.

    A live answer looks like ``{"type": "noul", "noul": 0.97}`` for a presence question and like
    ``{"choice": ..., "confidence": ..., "probabilities": {...}}`` for a Choice; reading only the
    Choice shape would report a confident ``noul`` as 0.0 and make a caller skip a clause the model
    was sure about.
    """

    probabilities = choice_probabilities(answer)
    if probabilities:
        return max(probabilities.values())
    for key in ("confidence", "noul", "score"):
        value = answer.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


__all__ = [
    "Clause",
    "ConnectionCandidate",
    "SymbolCandidate",
    "TypesafeSemanticPlanner",
    "candidate_symbols",
    "confidence_of",
    "connection_candidates",
    "free_port",
    "split_clauses",
]
