"""The second sentence edits the stored spec: additions, removals, and the full ledger.

What is proved here is the edit semantics -- the surface behaviour lives in
``test_text_edit_surface.py`` and the redraw boundary in ``test_m7_redraw.py``. The
discipline is the planner's: code reads the sentence, judgments only choose among
candidates, removals are data (a named tag is looked up, never guessed), and the output
is always the complete spec N+1 with every edit clause delivered or receipted.
"""

from __future__ import annotations

from agentcad.device_phrases import split_clauses
from agentcad.m7_diagram_spec import DiagramConnection, DiagramEntity, DiagramSpec, DiagramSystem
from agentcad.m7_text_edit import TypesafeSpecEditor
from agentcad.m7_text_planner import TypesafeDiagramSpecPlanner
from agentcad.symbols import SymbolRegistry
from agentcad.typesafe import TypesafeClient, TypesafeConfig

REGISTRY = SymbolRegistry()

BASE = DiagramSpec(
    label="base",
    systems=[DiagramSystem(system_id="S_main", name="主工艺系统", order=0)],
    entities=[
        DiagramEntity(
            engineering_id="el_V_101",
            kind="equipment",
            system_id="S_main",
            tag="V-101",
            name="缓冲罐",
            equipment_class="vessel",
            symbol_key="buffer_tank",
        ),
        DiagramEntity(
            engineering_id="el_P_101",
            kind="equipment",
            system_id="S_main",
            tag="P-101",
            name="燃料盐泵",
            equipment_class="pump",
            symbol_key="fuel_salt_pump",
        ),
    ],
    connections=[
        DiagramConnection(
            engineering_id="cn_1",
            source_engineering_id="el_V_101",
            target_engineering_id="el_P_101",
            medium="",
            tag="",
        )
    ],
)


class _Recorder:
    def __init__(self, answers: dict | None = None) -> None:
        self.answers = answers or {}
        self.payloads: list[dict] = []

    def __call__(self, config: TypesafeConfig, payload: dict) -> dict:
        self.payloads.append(payload)
        answered = {}
        for question, definition in payload["questions"].items():
            answered[question] = self.answers.get(
                question, {"choice": sorted(definition["criteria"])[0], "confidence": 0.9}
            )
        return {"model": "jev-latest", "usage": {"in": 1, "out": 1}, "answers": answered}


def _editor(recorder: _Recorder) -> TypesafeSpecEditor:
    return TypesafeSpecEditor(
        REGISTRY, client_factory=lambda config: TypesafeClient(config, transport=recorder)
    )


def _config() -> TypesafeConfig:
    return TypesafeConfig(api_key="test-key")


def test_adding_a_device_appends_to_the_base_and_keeps_everything_it_had() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "再添加一个缓冲罐 V-102", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "complete"
    assert plan.spec.problems() == []
    assert [entity.tag for entity in plan.spec.entities] == ["V-101", "P-101", "V-102"]
    assert len(plan.spec.connections) == 1
    assert plan.undelivered == ()


def test_a_new_device_may_connect_to_one_that_already_exists() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "再添加一个缓冲罐 V-102，把 V-102 接到 V-101",
        base_spec=BASE,
        typesafe_config=_config(),
    )
    assert plan.completeness == "complete"
    pairs = {(c.source_engineering_id, c.target_engineering_id) for c in plan.spec.connections}
    assert ("el_V_102", "el_V_101") in pairs
    assert ("el_V_101", "el_P_101") in pairs


def test_removing_a_device_drops_its_connections_as_a_named_consequence() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "删除 P-101", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "complete"
    assert [entity.tag for entity in plan.spec.entities] == ["V-101"]
    assert plan.spec.connections == []
    # the dropped connection is a consequence of the removal, named in the notes --
    # not an undelivered clause
    assert plan.undelivered == ()
    assert any("cn_1" in note for note in plan.notes)


def test_removing_a_tag_that_is_not_there_is_receipted_not_guessed() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "删除 P-999", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "partial"
    assert any("P-999" in item for item in plan.undelivered)
    assert [entity.tag for entity in plan.spec.entities] == ["V-101", "P-101"]


def test_adding_a_duplicate_tag_is_receipted_and_not_merged() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "再添加一个缓冲罐 V-101", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "partial"
    assert [entity.tag for entity in plan.spec.entities].count("V-101") == 1
    assert any("已有这个位号" in item for item in plan.undelivered)


def test_an_edit_connection_naming_an_unknown_tag_is_not_substituted() -> None:
    plan = _editor(_Recorder()).plan_edit(
        "把 V-101 接到 P-999", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "partial"
    assert plan.unknown_tags == ("P-999",)
    assert len(plan.spec.connections) == 1  # only the base connection survives
    assert any("把 V-101 接到 P-999" in item for item in plan.undelivered)


def test_every_edit_clause_is_delivered_or_receipted() -> None:
    recorder = _Recorder()
    plan = _editor(recorder).plan_edit(
        "添加一个仪表 FV-101，顺便把布局说清楚", base_spec=BASE, typesafe_config=_config()
    )
    assert plan.completeness == "partial"
    assert any("FV-101" in item for item in plan.undelivered)  # catalogue gap
    assert len(plan.catalog_gaps) == 1
    assert any("说清楚" in item or "布局" in item for item in plan.undelivered)
    # the untouched base is still whole
    assert {entity.tag for entity in plan.spec.entities} == {"V-101", "P-101"}


def test_remove_clauses_are_classified_by_code_not_by_a_judgment() -> None:
    assert [c.kind for c in split_clauses("添加一个塔 T-1，删除 T-1，把 T-1 接到 T-2")] == [
        "add",
        "remove",
        "connect",
    ]
    recorder = _Recorder()
    _editor(recorder).plan_edit("删除 P-101", base_spec=BASE, typesafe_config=_config())
    assert recorder.payloads == [], "a removal names data; no judgment is needed"


def test_the_editor_is_a_planner_with_the_same_no_geometry_boundary() -> None:
    import pathlib

    import agentcad.m7_text_edit as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("auto_layout_semantic", "m7_layout_contract", "agent_semantic_models"):
        assert forbidden not in source
    assert issubclass(TypesafeSpecEditor, TypesafeDiagramSpecPlanner)
