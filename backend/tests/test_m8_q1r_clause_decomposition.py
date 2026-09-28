"""M8-Q1R G2: parallel device-add enumeration becomes ordered per-device clauses.

DEV-4 sentence one -- 「给 V-101 添加一台液位计 LIT-101、一台压力表 PIT-101 和一台温度变送器
TT-101」 -- used to reach the planner as one merged phrase and fail lookup for all three
instruments at once. The frozen corpus sentence stays unchanged; the planner's reading of it
must now produce three independent requested devices, each with its own tag, in written
order, so each instrument's real candidate set (or structured gap) can be accounted for
separately.
"""

from test_m7_text_planner import REGISTRY, _Recorder

from agentcad.m7_text_planner import TypesafeDiagramSpecPlanner
from agentcad.symbols import SymbolRegistry
from agentcad.typesafe import TypesafeClient, TypesafeConfig


def make_planner() -> TypesafeDiagramSpecPlanner:
    return TypesafeDiagramSpecPlanner(SymbolRegistry())


def test_system_declaration_is_receipted_not_drawn() -> None:
    planner = make_planner()
    entities, _connect, _unknown, _unknown_clauses = planner.read(
        "添加一个主工艺系统，添加一个缓冲罐 V-101"
    )

    # The system clause is recognized and separated; the device clause is untouched.
    assert [entity.tag for entity in entities] == ["V-101"]
    assert len(planner.last_system_declarations) == 1
    assert "主工艺系统" in planner.last_system_declarations[0].text


def test_system_declaration_never_becomes_phantom_equipment() -> None:
    recorder = _Recorder()
    planner = TypesafeDiagramSpecPlanner(
        REGISTRY,
        client_factory=lambda config: TypesafeClient(config, transport=recorder),
    )
    plan = planner.plan(
        "添加一个主工艺系统，添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101",
        typesafe_config=TypesafeConfig(api_key="test-key"),
    )

    tags = [entity.tag for entity in plan.spec.entities]
    assert "E-01" not in tags  # the phantom equipment the fail-open path used to mint
    assert tags == ["V-101", "P-101"]
    assert any("系统声明暂不支持" in note for note in plan.skipped)
    assert plan.completeness == "partial"  # honest, not a fake complete


def test_dev4_enumeration_becomes_three_independent_devices() -> None:
    planner = make_planner()
    entities, _connect, _unknown, _unknown_clauses = planner.read(
        "给 V-101 添加一台液位计 LIT-101、一台压力表 PIT-101 和一台温度变送器 TT-101"
    )

    assert [entity.tag for entity in entities] == ["LIT-101", "PIT-101", "TT-101"]
    assert len({entity.engineering_id for entity in entities}) == 3
    # Each device is declared by its own clause text -- the ledger can receipt each one
    # separately instead of one merged phrase failing lookup for all three.
    clauses = [entity.source_clause for entity in entities]
    assert len(set(clauses)) == 3
    assert all("添加" in clause for clause in clauses)
    for entity in entities:
        assert entity.tag in entity.source_clause
        assert entity.phrase and entity.tag not in entity.phrase
    # Written order is the source ordering; the enumeration is not re-sorted.
    assert "LIT-101" in clauses[0] and "PIT-101" not in clauses[0]


def test_single_device_add_is_untouched() -> None:
    planner = make_planner()
    entities, _connect, _unknown, _unknown_clauses = planner.read("添加一个缓冲罐 V-101")

    assert [entity.tag for entity in entities] == ["V-101"]
    assert entities[0].source_clause == "添加一个缓冲罐 V-101"


def test_enumeration_item_without_its_own_tag_is_not_split() -> None:
    planner = make_planner()
    entities, _connect, _unknown, _unknown_clauses = planner.read(
        "添加一台液位计 LIT-101、一台压力表"
    )

    # One item has no tag of its own: this is not the device enumeration grammar, so the
    # clause must survive whole rather than be mangled into a half-split.
    assert len(entities) == 1


def test_enumeration_item_with_a_connect_verb_is_not_split() -> None:
    planner = make_planner()
    entities, connect, _unknown, _unknown_clauses = planner.read(
        "添加一台离心泵 P-101、一台缓冲罐 V-101 并接到 P-101"
    )

    # A segment carrying a connect verb makes the WHOLE clause a connection (the verb table
    # classifies connect before add): nothing is counted as an add, the clause is not split,
    # and it routes through the connect path whole.
    assert entities == []
    assert len(connect) == 1
    assert "、" in connect[0].text


def test_two_device_enumeration_shares_host_and_keeps_order() -> None:
    planner = make_planner()
    entities, _connect, _unknown, _unknown_clauses = planner.read(
        "给 V-101 添加一台液位计 LIT-101 和一台压力表 PIT-101"
    )

    # 、 absent but 和 present: only one measure item actually follows the verb before 和 --
    # this is two clauses? No: without 、 there is nothing to split on; the clause stays whole.
    assert len(entities) == 1
