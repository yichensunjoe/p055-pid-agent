"""One sentence, one specification -- and the sentence never becomes a coordinate.

Grouped by what each part promises: the sentence is read into candidates by code, the judgments only
choose among those candidates, an uncertain or unknown clause is reported rather than invented, and
the specification that comes out is one the deterministic chain can actually draw.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad.auto_layout_canvas import derive_semantic_canvas
from agentcad.auto_layout_geometry import (
    annotate_semantic_layout,
    materialize_semantic_layout,
    route_semantic_layout,
)
from agentcad.auto_layout_identity import finalize_semantic_layout
from agentcad.auto_layout_semantic import (
    STEP_4,
    STEP_5,
    place_semantic_layout,
    plan_semantic_layout,
)
from agentcad.device_phrases import candidate_symbols, split_clauses
from agentcad.m7_diagram_adapter import adapt
from agentcad.m7_diagram_spec import load_diagram_spec, reject_geometry
from agentcad.m7_layout_materialization import (
    LABEL_ELEMENT_ROLE,
    materialize_canonical_layout,
)
from agentcad.m7_symbol_geometry import freeze_symbol_geometry
from agentcad.m7_text_planner import (
    UNTAGGED_TAG_TEMPLATE,
    DiagramSpecPlanningError,
    TypesafeDiagramSpecPlanner,
)
from agentcad.symbols import SymbolRegistry
from agentcad.typesafe import TypesafeClient, TypesafeConfig

REGISTRY = SymbolRegistry()

SENTENCE = "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"


class _Recorder:
    """The service's shape, without the network: answers come from a table, and are recorded."""

    def __init__(self, answers: dict[str, dict] | None = None, *, model: str = "jev-latest"):
        self.answers = answers or {}
        self.model = model
        self.payloads: list[dict] = []

    def __call__(self, config: TypesafeConfig, payload: dict) -> dict:
        self.payloads.append(payload)
        answered = {question: self._answer(payload, question) for question in payload["questions"]}
        return {"model": self.model, "usage": {"in": 10, "out": 2}, "answers": answered}

    def _answer(self, payload: dict, question: str) -> dict:
        if question in self.answers:
            return self.answers[question]
        definition = payload["questions"][question]
        return {"choice": sorted(definition["criteria"])[0], "confidence": 0.9}


def _planner(recorder: _Recorder, **kwargs) -> TypesafeDiagramSpecPlanner:
    return TypesafeDiagramSpecPlanner(
        REGISTRY, client_factory=lambda config: TypesafeClient(config, transport=recorder), **kwargs
    )


def _config() -> TypesafeConfig:
    return TypesafeConfig(api_key="test-key")


# ---------------------------------------------------------------------------------------------
# Reading the sentence: what code decides without asking
# ---------------------------------------------------------------------------------------------


def test_the_sentence_is_split_and_classified_by_code_not_by_a_judgment() -> None:
    clauses = split_clauses(SENTENCE)
    assert [clause.kind for clause in clauses] == ["add", "add", "connect"]
    assert clauses[-1].text.startswith("把")


def test_a_tag_the_sentence_spells_out_is_data_the_model_never_sees() -> None:
    """The tags come from a pattern, so a judgment can neither invent nor mistype one."""

    planner = _planner(_Recorder())
    entities, _connections, unknown, _unknown_clauses = planner.read(SENTENCE)
    assert [entity.tag for entity in entities] == ["V-101", "P-101"]
    assert [entity.engineering_id for entity in entities] == ["el_V_101", "el_P_101"]
    # The phrase reaches the judgment as the device words alone: no verb, no tag.
    assert [entity.phrase for entity in entities] == ["缓冲罐", "燃料盐泵"]
    assert unknown == ()


def test_an_untagged_device_gets_a_deterministic_positional_tag() -> None:
    planner = _planner(_Recorder())
    entities, _connections, _unknown, _unknown_clauses = planner.read("添加一台离心泵，添加一个塔")
    assert [entity.tag for entity in entities] == [
        UNTAGGED_TAG_TEMPLATE.format(index=1),
        UNTAGGED_TAG_TEMPLATE.format(index=2),
    ]


def test_a_connection_that_names_two_tags_is_pinned_by_code() -> None:
    """Order is the only thing in the sentence that says which end is the source."""

    planner = _planner(_Recorder())
    entities, clauses, _unknown, _unknown_clauses = planner.read(SENTENCE)
    candidates = planner.connection_candidates(entities, clauses[0])
    assert candidates == (("el_V_101", "el_P_101"),)


def test_a_connection_that_names_no_tag_offers_the_ordered_pairs() -> None:
    planner = _planner(_Recorder())
    entities, clauses, _unknown, _unknown_clauses = planner.read("添加一台泵，添加一个塔，把泵连到塔")
    candidates = planner.connection_candidates(entities, clauses[0])
    assert set(candidates) == {("el_E_01", "el_E_02"), ("el_E_02", "el_E_01")}


def test_a_tag_no_device_declares_is_reported_rather_than_invented() -> None:
    """A typo must be visible. Turning it into a new device would be inventing plant."""

    planner = _planner(_Recorder())
    _entities, _clauses, unknown, _unknown_clauses = planner.read("添加一个过滤器 F-201，把 F-201 接到 P-999")
    assert unknown == ("P-999",)


# ---------------------------------------------------------------------------------------------
# The completeness account: coherent is not complete, and no clause disappears silently
# ---------------------------------------------------------------------------------------------


def test_a_fully_delivered_sentence_is_complete() -> None:
    plan = _planner(_Recorder()).plan(SENTENCE, typesafe_config=_config())
    assert plan.completeness == "complete"
    assert plan.undelivered == ()


def test_a_skipped_device_makes_the_plan_partial_and_receipts_everything_it_dropped() -> None:
    recorder = _Recorder({"el_P_101": {"choice": "centrifugal_pump", "confidence": 0.12}})
    plan = _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    assert plan.completeness == "partial"
    # The skipped device and the connection that needed it are both receipted: the ledger
    # names what the sentence asked for and the spec does not carry.
    assert any("P-101" in item for item in plan.undelivered)
    assert any("把 V-101 接到 P-101" in item for item in plan.undelivered)
    # ...while the spec itself stays structurally coherent, which is exactly the trap: a
    # coherent spec that is not the whole sentence.
    assert plan.spec.problems() == []


def test_an_unknown_tag_makes_the_plan_partial() -> None:
    plan = _planner(_Recorder()).plan(
        "添加一个缓冲罐 V-101，把 V-101 接到 P-999", typesafe_config=_config()
    )
    assert plan.unknown_tags == ("P-999",)
    assert plan.completeness == "partial"


def test_an_unknown_clause_is_receipted_not_silently_dropped() -> None:
    plan = _planner(_Recorder()).plan(SENTENCE + "，说明一下画布", typesafe_config=_config())
    assert plan.completeness == "partial"
    assert any("说明一下画布" in item for item in plan.undelivered)


def test_a_catalogue_gap_is_reported_never_widened_into_a_judgment() -> None:
    """「仪表」has no symbol in the catalogue. The gap is receipted, and no question is asked
    that would let the model pick a look-alike the sentence did not name."""

    recorder = _Recorder()
    plan = _planner(recorder).plan(
        "添加一个缓冲罐 V-101，添加一个仪表 FV-101，把 V-101 接到 FV-101",
        typesafe_config=_config(),
    )
    for payload in recorder.payloads:
        assert "el_FV_101" not in payload["questions"]
    assert "FV-101" not in [entity.tag for entity in plan.spec.entities]
    assert plan.completeness == "partial"
    assert any("目录" in item and "FV-101" in item for item in plan.undelivered)


def test_every_clause_is_delivered_or_receipted_in_sentence_order() -> None:
    """The ledger plus the spec covers the input exactly: three clauses in, three accounted."""

    plan = _planner(_Recorder()).plan(
        "添加一个塔 T-101，添加一个仪表 FV-101，随便说说", typesafe_config=_config()
    )
    assert plan.completeness == "partial"
    # two receipts: the catalogue-gapped device and the unrecognised clause; the tower is
    # delivered and must not appear in the ledger.
    assert any("FV-101" in item for item in plan.undelivered)
    assert any("随便说说" in item for item in plan.undelivered)
    assert not any("T-101" in item for item in plan.undelivered)
    assert [entity.tag for entity in plan.spec.entities] == ["T-101"]


# ---------------------------------------------------------------------------------------------
# The judgments: one per undecided choice, and the answer is an index into the candidates
# ---------------------------------------------------------------------------------------------


def test_a_kind_the_catalogue_spells_once_is_a_lookup_with_no_judgment() -> None:
    """「塔」 has exactly one symbol, so asking a model would only add a way to be wrong."""

    recorder = _Recorder()
    assert len(candidate_symbols(REGISTRY, "塔")) == 1
    plan = _planner(recorder).plan("添加一个塔 T-101", typesafe_config=_config())
    assert recorder.payloads == []  # no request was made at all
    assert plan.judgment_count == 0
    assert plan.entities[0].decided_by == "lookup"
    assert plan.spec.entities[0].symbol_key == candidate_symbols(REGISTRY, "塔")[0].key


def test_a_device_phrase_asks_one_question_whose_criteria_are_the_candidates() -> None:
    recorder = _Recorder()
    plan = _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    assert len(recorder.payloads) == 1  # every undecided choice, asked together
    payload = recorder.payloads[0]
    # The two adds need a judgment each; the connect was pinned by code, so it asks nothing.
    assert set(payload["questions"]) == {"el_V_101", "el_P_101"}
    criteria = payload["questions"]["el_P_101"]["criteria"]
    assert set(criteria) == {row.key for row in candidate_symbols(REGISTRY, "燃料盐泵")}
    # The engine's own symbol key is in the criteria, so the answer is a choice among spellings
    # rather than free text that a later step would have to validate.
    assert plan.spec.entities[1].symbol_key in criteria
    assert "api_key" not in json.dumps(payload)


def test_the_state_carries_the_phrase_and_the_tag_and_never_a_key() -> None:
    recorder = _Recorder()
    _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    state = recorder.payloads[0]["state"]
    assert state["el_P_101"] == {"phrase": "燃料盐泵", "tag": "P-101"}
    assert state["request"]["tags"] == ["V-101", "P-101"]
    assert "test-key" not in json.dumps(recorder.payloads[0])


def test_an_answer_outside_the_candidates_cannot_become_a_symbol() -> None:
    """The model cannot name a device the catalogue does not carry: it can only choose a candidate."""

    recorder = _Recorder({"el_P_101": {"choice": "fusion_reactor", "confidence": 0.99}})
    plan = _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    assert [entity.symbol_key for entity in plan.spec.entities] == [
        sorted(recorder.payloads[0]["questions"]["el_V_101"]["criteria"])[0]
    ]
    assert not any(entity.symbol_key == "fusion_reactor" for entity in plan.spec.entities)
    assert "P-101" not in [entity.tag for entity in plan.spec.entities]


def test_an_uncertain_choice_is_reported_and_skipped_rather_than_drawn() -> None:
    recorder = _Recorder({"el_P_101": {"choice": "centrifugal_pump", "confidence": 0.12}})
    plan = _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    assert [entity.tag for entity in plan.spec.entities] == ["V-101"]
    assert any("阈值" in note for note in plan.skipped)
    # The skipped device is named, and the connection that needed it is dropped rather than
    # pointing at an identity the specification no longer declares.
    assert plan.spec.connections == []
    assert plan.spec.problems() == []


def test_a_connection_leading_no_declared_device_is_dropped_and_the_spec_stays_coherent() -> None:
    recorder = _Recorder({"el_P_101": {"choice": "centrifugal_pump", "confidence": 0.12}})
    plan = _planner(recorder).plan(SENTENCE, typesafe_config=_config())
    assert len(plan.connections) == 1
    assert plan.connections[0].chosen == ("el_V_101", "el_P_101")  # the judgment did happen
    assert plan.spec.problems() == []


# ---------------------------------------------------------------------------------------------
# The specification: no coordinates, always coherent, and always drawable
# ---------------------------------------------------------------------------------------------


def test_the_specification_carries_no_geometry_at_all() -> None:
    plan = _planner(_Recorder()).plan(SENTENCE, typesafe_config=_config())
    payload = plan.spec.model_dump(mode="json")
    reject_geometry(payload)  # raises if any coordinate-shaped field survived
    assert load_diagram_spec(payload).problems() == []
    assert plan.spec.systems[0].system_id == "S_main"
    assert all(entity.system_id == "S_main" for entity in plan.spec.entities)


def test_the_same_sentence_and_the_same_answers_produce_the_same_specification() -> None:
    first = _planner(_Recorder()).plan(SENTENCE, typesafe_config=_config())
    second = _planner(_Recorder()).plan(SENTENCE, typesafe_config=_config())
    assert first.spec == second.spec
    assert first.spec.model_dump(mode="json") == second.spec.model_dump(mode="json")


def test_a_sentence_with_no_device_is_refused_with_the_sentence_in_the_message() -> None:
    with pytest.raises(DiagramSpecPlanningError) as raised:
        _planner(_Recorder()).plan("把所有的东西都连起来", typesafe_config=_config())
    assert "把所有的东西都连起来" in str(raised.value.detail()) + str(raised.value)


def _finalized(spec) -> object:
    """The whole deterministic chain, as production runs it: spec -> step 5, no coordinates in."""

    topology = adapt(load_diagram_spec(spec.model_dump(mode="json")))
    snapshot = freeze_symbol_geometry(
        entity.symbol_key for entity in spec.entities
    )
    staged = plan_semantic_layout(topology)
    placed = place_semantic_layout(staged)
    materialized = materialize_semantic_layout(placed, snapshot)
    routed = route_semantic_layout(materialized, snapshot)
    annotated = annotate_semantic_layout(
        routed, {entity.engineering_id: entity.tag for entity in spec.entities}
    )
    canvassed = derive_semantic_canvas(annotated)
    assert canvassed.produced_at_step == STEP_4
    finalized = finalize_semantic_layout(canvassed, topology)
    assert finalized.produced_at_step == STEP_5
    assert finalized.canonical_layout_digest
    return finalized


def test_the_generated_specification_reaches_the_finalized_canonical_layout() -> None:
    """The point of the whole step: a sentence becomes a layout the deterministic chain drew."""

    plan = _planner(_Recorder()).plan(SENTENCE, typesafe_config=_config())
    finalized = _finalized(plan.spec)

    layout = materialize_canonical_layout(finalized, document_id="doc_nl")
    labels = {
        row["engineering_id"]: row["text"] for row in layout.rows if row["kind"] == "annotation"
    }
    # The drawn words are the tags the sentence wrote, derived rather than supplied.
    assert labels == {"el_V_101": "V-101", "el_P_101": "P-101"}
    assert layout.element_id_for("el_V_101", LABEL_ELEMENT_ROLE).startswith("el_label_")
    assert len(layout.operations) == len(layout.rows) + len(finalized.engineering_systems)


def test_the_planner_does_not_import_the_layout_chain_or_the_legacy_semantic_plan() -> None:
    """It produces a specification. Deciding what to do with one is somebody else's job."""

    import agentcad.m7_text_planner as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "auto_layout_semantic",
        "m7_layout_contract",
        "m7_layout_materialization",
        "agent_semantic_models",
        "SemanticTransaction",
    ):
        assert forbidden not in source
