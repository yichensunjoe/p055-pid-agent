"""M7-2 phase 2B step 1: the engine's semantic-topology ingress, at the door.

The claim this module tests is narrow on purpose. The engine now has a way in that takes the
*semantic* input contract, receives the discrete intent, and places nothing. It does not yet
claim that a plant drawing comes out well -- that is what steps 2 to 5 are for, and mixing the
two claims is how "the layout is bad" stops being attributable.

Three things are worth more than the rest here:

* **The topology arrives as topology.** A payload or a document is refused by type. The
  tempting alternative -- give every node ``(0, 0)`` and call the old document-shaped entry
  point -- would move coordinate authority from the model to the ingress and bring the original
  defect back.
* **The ingress never touches the service.** It is handed a service whose ``get_document``
  raises, and it still produces a plan: a step that needed a document would be a second
  document path, not an ingress.
* **Nothing is applied silently.** Every declared intent dimension is accounted for, and the
  ones whose effect lands in a later step are *named* as pending rather than quietly skipped.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from agentcad.auto_layout import AutoLayoutEngine as BaseAutoLayoutEngine
from agentcad.auto_layout_engine import AutoLayoutEngine
from agentcad.auto_layout_semantic import (
    INGRESS_STEP,
    LAYOUT_ENGINE_VERSION,
    LAYOUT_RULES_VERSION,
    SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION,
    SemanticLayoutPlan,
    SemanticTopologyIngressError,
    plan_digest,
    plan_semantic_layout,
)
from agentcad.layout_models import AutoLayoutRequest
from agentcad.m7_diagram_adapter import adapt, topology_digest
from agentcad.m7_layout_contract import (
    CANVAS_DERIVATION_CHAIN,
    FORBIDDEN_MODEL_GEOMETRY_FIELDS,
    LAYOUT_ACCEPTANCE_FIXTURES,
    LAYOUT_DIGEST_INPUTS,
    LAYOUT_INTENT_CONSUMPTION,
    PHASE_2B_STEPS,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

TASK_BOOK = Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"


def fixture_a_payload() -> dict:
    """Fixture A, imported from the phase-2A module so the two phases cannot drift apart."""

    from test_m7_diagram_adapter import fixture_a_payload as payload

    return payload()


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "ingress.db"), SymbolRegistry())


class _DocumentReadingForbiddenService:
    """A service that fails loudly if the ingress asks it for a document."""

    symbols = SymbolRegistry()

    def get_document(self, document_id: str):  # pragma: no cover - it must never be called
        raise AssertionError(
            f"the semantic ingress asked for document {document_id!r}: a step that reads a "
            "document is not adapting a topology, it is rerouting the manual path"
        )


# --------------------------------------------------------------------------------------
# The door: who it is attached to, and what it refuses
# --------------------------------------------------------------------------------------


def test_the_ingress_is_a_method_on_the_one_layout_authority() -> None:
    """Both the base engine and the hardened subclass the application uses carry it."""

    assert hasattr(BaseAutoLayoutEngine, "layout_semantic_topology")
    assert hasattr(AutoLayoutEngine, "layout_semantic_topology")
    assert issubclass(AutoLayoutEngine, BaseAutoLayoutEngine)
    assert (
        AutoLayoutEngine.layout_semantic_topology is BaseAutoLayoutEngine.layout_semantic_topology
    )


def test_the_ingress_has_no_preserve_positions_parameter() -> None:
    """The policy is enforced by the signature, so there is nothing for a caller to override.

    On the manual path `preserve_positions` is a field of ``AutoLayoutRequest``. The semantic
    ingress takes neither that request nor such a keyword, which is what "the caller cannot
    override it" means in practice.
    """

    signature = inspect.signature(BaseAutoLayoutEngine.layout_semantic_topology)
    assert "preserve_positions" in AutoLayoutRequest.model_fields
    assert "preserve_positions" not in signature.parameters
    with pytest.raises(TypeError):
        AutoLayoutEngine.__new__(AutoLayoutEngine).layout_semantic_topology(
            adapt(fixture_a_payload()), preserve_positions=True
        )
    with pytest.raises(SemanticTopologyIngressError):
        plan_semantic_layout(AutoLayoutRequest())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ({"systems": [], "entities": []}, "dict"),
        ("m7-diagram-spec/1", "str"),
        ([], "list"),
        (object(), "object"),
    ],
)
def test_the_ingress_refuses_anything_that_is_not_the_input_contract(
    value: object, named: str
) -> None:
    """Refused by type, not coerced: an engine that adapted a payload would be a second adapter."""

    with pytest.raises(SemanticTopologyIngressError) as failure:
        plan_semantic_layout(value)  # type: ignore[arg-type]

    assert named in str(failure.value)
    assert "SemanticTopology" in str(failure.value)


def test_the_ingress_produces_a_plan_without_reading_a_document(tmp_path: Path) -> None:
    """A plan from a topology, with a service that would raise if it were consulted."""

    engine = AutoLayoutEngine(_DocumentReadingForbiddenService())  # type: ignore[arg-type]
    plan = engine.layout_semantic_topology(adapt(fixture_a_payload()))

    assert isinstance(plan, SemanticLayoutPlan)
    assert plan.produced_at_step == INGRESS_STEP


# --------------------------------------------------------------------------------------
# The plan is an intermediate, and says so
# --------------------------------------------------------------------------------------


def test_the_plan_places_nothing_and_produces_no_canvas() -> None:
    plan = plan_semantic_layout(adapt(fixture_a_payload()))

    assert plan.placement == ()
    assert plan.canvas_bounds is None
    assert plan.preserve_positions is False
    assert INGRESS_STEP == "step_1"
    assert PHASE_2B_STEPS[-1][1] == "canonical_projection_and_layout_digest"
    assert CANVAS_DERIVATION_CHAIN[-1] == "canonical_layout_digest"


def test_the_plan_contains_no_geometry_field_at_all() -> None:
    """The same negative gate as phase 2A, applied to the plan the engine will place from."""

    plan = plan_semantic_layout(adapt(fixture_a_payload()))
    rendered = json.dumps(plan.to_projection(), ensure_ascii=False)

    for field in FORBIDDEN_MODEL_GEOMETRY_FIELDS:
        assert f'"{field}"' not in rendered, field
    assert 'canvas_bounds":null' in rendered.replace(" ", "")


def test_the_plan_reports_the_engine_and_rules_version_it_ran_under() -> None:
    plan = plan_semantic_layout(adapt(fixture_a_payload()))

    assert plan.engine_version == LAYOUT_ENGINE_VERSION
    assert plan.rules_version == LAYOUT_RULES_VERSION
    assert LAYOUT_ENGINE_VERSION and LAYOUT_RULES_VERSION
    # The digest that step 5 produces reads these under the declared field names.
    assert "layout_engine_version" in LAYOUT_DIGEST_INPUTS
    assert "layout_rules_version" in LAYOUT_DIGEST_INPUTS


def test_the_plan_digest_is_not_the_canonical_layout_digest() -> None:
    plan = plan_semantic_layout(adapt(fixture_a_payload()))

    assert plan.digest_version == SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION
    assert SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION not in LAYOUT_DIGEST_INPUTS
    assert plan.digest == plan_digest(plan)
    assert plan.digest != plan.topology_digest


# --------------------------------------------------------------------------------------
# Intent: received at the ingress, applied later, never silently ignored
# --------------------------------------------------------------------------------------


def test_every_declared_intent_dimension_is_received_and_accounted_for() -> None:
    plan = plan_semantic_layout(adapt(fixture_a_payload()))
    contract_dimensions = {item.dimension for item in LAYOUT_INTENT_CONSUMPTION}

    assert {use.dimension for use in plan.intent_uses} == contract_dimensions
    assert all(use.received_at_step == "step_1" for use in plan.intent_uses)
    assert all(use.behaviour.strip() for use in plan.intent_uses)
    assert {use.applied_at_step for use in plan.intent_uses} <= {key for key, _ in PHASE_2B_STEPS}


def test_the_dimensions_that_apply_later_are_named_as_pending_rather_than_skipped() -> None:
    """`declared but not yet applied` is visible. At step 1 that is all six dimensions, and the
    plan says so instead of looking finished."""

    plan = plan_semantic_layout(adapt(fixture_a_payload()))

    assert plan.pending_intent_dimensions == (
        "orientation",
        "preferred_aspect_class",
        "primary_flow_direction",
        "system_order",
        "grouping",
        "density",
    )


def test_the_intent_arrives_unchanged_from_the_topology() -> None:
    topology = adapt(fixture_a_payload())
    plan = plan_semantic_layout(topology)

    assert plan.intent.orientation == topology.intent.orientation
    assert plan.intent.preferred_aspect_class == topology.intent.preferred_aspect_class
    assert plan.intent.primary_flow_direction == topology.intent.primary_flow_direction
    assert plan.intent.grouping == topology.intent.grouping
    assert plan.intent.density == topology.intent.density
    assert plan.intent.system_order == topology.intent.system_order


def test_a_discrete_intent_change_changes_the_plan_digest() -> None:
    """Intent is part of the plan's identity: two runs that were asked for different drawings
    are not the same run."""

    payload = fixture_a_payload()
    payload["layout_intent"]["preferred_aspect_class"] = "standard"

    first = plan_digest(plan_semantic_layout(adapt(fixture_a_payload())))
    second = plan_digest(plan_semantic_layout(adapt(payload)))

    assert first != second


def test_the_reading_order_names_the_declared_systems_first() -> None:
    """An ordering, not a partition: what was declared, then what was declared later."""

    payload = fixture_a_payload()
    payload["layout_intent"]["system_order"] = ["S_cover"]
    plan = plan_semantic_layout(adapt(payload))

    assert plan.systems_in_reading_order == ("S_cover", "S_supply")
    # The order the specification declared decides the rest, not the order they were listed in.
    payload["systems"] = list(reversed(payload["systems"]))
    assert plan_semantic_layout(adapt(payload)).systems_in_reading_order == (
        "S_cover",
        "S_supply",
    )


def test_every_node_is_accounted_for_exactly_once_in_the_partition() -> None:
    topology = adapt(fixture_a_payload())
    plan = plan_semantic_layout(topology)

    flat = [node_id for _, node_ids in plan.node_ids_by_system for node_id in node_ids]
    assert sorted(flat) == sorted(node.engineering_id for node in topology.nodes)
    assert [system_id for system_id, _ in plan.node_ids_by_system] == list(
        plan.systems_in_reading_order
    )
    assert plan.node_ids_by_system == (
        ("S_supply", ("el_ar_tank", "el_pt_101", "el_purifier")),
        ("S_cover", ("el_recycle",)),
    )


def test_the_required_loops_survive_the_ingress() -> None:
    topology = adapt(fixture_a_payload())
    plan = plan_semantic_layout(topology)

    assert plan.required_loops == (("loop_cover_gas", ("el_ar_tank", "el_purifier", "el_recycle")),)


# --------------------------------------------------------------------------------------
# Determinism: the same topology is the same plan
# --------------------------------------------------------------------------------------


def test_the_same_topology_produces_the_same_plan_digest() -> None:
    first = plan_semantic_layout(adapt(fixture_a_payload()))
    second = plan_semantic_layout(adapt(fixture_a_payload()))

    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert first.topology_digest == second.topology_digest


def test_listing_order_does_not_change_the_plan_digest() -> None:
    shuffled = fixture_a_payload()
    shuffled["systems"] = list(reversed(shuffled["systems"]))
    shuffled["entities"] = list(reversed(shuffled["entities"]))

    assert plan_digest(plan_semantic_layout(adapt(shuffled))) == plan_digest(
        plan_semantic_layout(adapt(fixture_a_payload()))
    )


def test_the_plan_repeats_the_topology_digest_it_was_given() -> None:
    """The ingress identifies its input rather than re-deriving it, so a plan traced back to a
    specification can be checked against the adapter's own digest."""

    topology = adapt(fixture_a_payload())
    plan = plan_semantic_layout(topology)

    assert plan.topology_digest == topology_digest(topology)


# --------------------------------------------------------------------------------------
# Still no surface, and the legacy path still works
# --------------------------------------------------------------------------------------


def test_the_ingress_is_not_reachable_from_any_route_or_tool() -> None:
    from fastapi.testclient import TestClient

    from agentcad.main import create_app

    app = create_app()
    with TestClient(app):
        paths = set(app.openapi()["paths"])
    assert not [path for path in paths if "semantic_topology" in path or "diagram" in path]
    mcp_source = (Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert "layout_semantic_topology" not in mcp_source


def test_the_manual_path_keeps_its_preserve_positions_and_still_places(tmp_path: Path) -> None:
    """M7-2 does not break editing to make drawing work: the document path is untouched, and it
    is the same engine class that serves both paths."""

    assert AutoLayoutRequest().preserve_positions is False
    assert AutoLayoutRequest(preserve_positions=True).preserve_positions is True
    engine = AutoLayoutEngine(make_service(tmp_path))
    assert engine.layout_semantic_topology(adapt(fixture_a_payload())).placement == ()
    # The legacy entry points are still there, still document-shaped, and still the same engine.
    assert hasattr(engine, "preview")
    assert hasattr(engine, "preview_document")
    assert "preserve_positions" in AutoLayoutRequest.model_fields


def test_the_task_book_and_the_acceptance_fixtures_still_name_this_step() -> None:
    """The step is declared in the task book, and the fixtures this phase is measured against
    are the ones the contract declared in phase 1 -- not a new, easier set."""

    task_book = TASK_BOOK.read_text(encoding="utf-8")
    assert "PHASE_2B_STEPS" in task_book
    assert "layout_semantic_topology" in task_book
    assert [fixture.key for fixture in LAYOUT_ACCEPTANCE_FIXTURES] == ["A", "B", "C", "D"]
