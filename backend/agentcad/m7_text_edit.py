"""The second sentence: a semantic edit applied to the stored spec, never to pixels.

``text-plan`` creates; this module edits. The discipline is unchanged -- code reads the
sentence into candidates, System One judges only what a lookup cannot decide, and the
model's answer is always a choice among candidates, never a diff text -- but the base the
sentence lands on is the stored :class:`DiagramSpec`, not a drawing:

* **Additions** (add/connect clauses) resolve exactly like a fresh plan, except connection
  candidates may also name devices that already exist in the base spec: 「把 V-101 接到
  新加的塔 T-201」 pins one end to the stored V-101 and asks about the other.
* **Removals** are data, not judgments: the tag the clause spells out is looked up in the
  base spec. Found -> the entity and every connection through it leave; a connection that
  disappears because an endpoint was removed is a consequence named in the notes, not an
  undelivered clause. Not found -> the clause is receipted; the model is never asked to
  guess what the user meant to delete.

The output is always the **complete** spec N+1 plus the same completeness ledger the
planner keeps: every clause of the edit sentence is delivered or receipted, a catalogue
gap is reported, never substituted, and a partial edit may be previewed but never
committed by the surface.
"""

from __future__ import annotations

from .device_phrases import Clause, available_alternatives, extract_tags, split_clauses
from .m7_diagram_spec import DiagramConnection, DiagramEntity, DiagramSpec
from .m7_synthesis_contract import Completeness
from .m7_text_planner import (
    PlannedConnection,
    PlannedDiagram,
    PlannedEntity,
    TypesafeDiagramSpecPlanner,
    _is_equipment,
)


class TypesafeSpecEditor(TypesafeDiagramSpecPlanner):
    """Plans an edit against a stored spec, reusing the planner's candidate discipline."""

    def plan_edit(self, prompt: str, *, base_spec: DiagramSpec, typesafe_config) -> PlannedDiagram:
        connect_clauses = [c for c in split_clauses(prompt) if c.kind == "connect"]
        remove_clauses = [c for c in split_clauses(prompt) if c.kind == "remove"]
        unknown_clauses = [c for c in split_clauses(prompt) if c.kind == "unknown"]

        # Read only the *delta* devices the edit sentence declares; the base spec's
        # entities join the candidate pool as already-resolved facts.
        delta_entities, _base_connects, _sentence_unknown, _unknown = self.read(prompt)
        base_entities = [
            PlannedEntity(
                engineering_id=entity.engineering_id,
                tag=entity.tag,
                phrase=entity.name,
                candidates=(),
                chosen_symbol_key=entity.symbol_key,
                confidence=1.0,
                decided_by="lookup",
                source_clause="",
                catalog_gap=False,
            )
            for entity in base_spec.entities
        ]
        pool = [*base_entities, *delta_entities]
        # Connection tags may name devices that already exist in the base spec, so the
        # unknown-tag set is pool-scoped, not sentence-scoped: a tag the *edit* does not
        # declare is still known when the *drawing* carries it.
        mentioned = {
            tag for clause in connect_clauses for tag in extract_tags(clause.text)
        }
        unknown_tags = tuple(sorted(mentioned - {entity.tag for entity in pool}))

        connections = [
            PlannedConnection(
                engineering_id=f"cn_{index}",
                phrase=clause.text,
                endpoints=("", ""),
                candidates=self.connection_candidates(pool, clause),
            )
            for index, clause in enumerate(connect_clauses, start=1)
        ]

        notes: list[str] = []
        skipped: list[str] = []
        state, questions = self.questions(delta_entities, connections)
        if questions:
            result = self.client_factory(typesafe_config).judge(state, questions)
            answers = result["answers"]
            model = str(result.get("model", ""))
            latency = float(result.get("latency_ms", 0.0))
            judgments = len(questions)
        else:
            answers = {}
            model = ""
            latency = 0.0
            judgments = 0

        delta_entities = [
            self._resolve_entity(entity, answers.get(entity.engineering_id), notes, skipped)
            for entity in delta_entities
        ]
        resolved_pool = {entity.engineering_id: entity for entity in [*base_entities, *delta_entities]}
        labels = {eid: entity.tag for eid, entity in resolved_pool.items()}
        connections = [
            self._resolve_connection(
                connection, answers.get(connection.engineering_id), labels, notes, skipped
            )
            for connection in connections
        ]

        spec, undelivered, catalog_gaps = self._apply_edit(
            base_spec=base_spec,
            delta_entities=delta_entities,
            delta_connections=connections,
            remove_clauses=remove_clauses,
            unknown_clauses=unknown_clauses,
            unknown_tags=unknown_tags,
            notes=notes,
            skipped=skipped,
        )
        problems = spec.problems()
        if problems:
            raise self._incoherent(problems)

        resolved_count = len(spec.entities)
        if resolved_count == 0:
            completeness: Completeness = "empty"
        elif skipped or unknown_tags or undelivered:
            completeness = "partial"
        else:
            completeness = "complete"
        notes.insert(
            0,
            f"TypeSafe 判读：{judgments} 个判断（{len(delta_entities)} 个新设备、"
            f"{len(connections)} 条连接、{len(remove_clauses)} 条删除），"
            f"完整度：{completeness}。",
        )
        return PlannedDiagram(
            spec=spec,
            entities=tuple(delta_entities),
            connections=tuple(connections),
            notes=tuple(notes),
            skipped=tuple(skipped),
            model=model,
            latency_ms=latency,
            question_count=len(questions),
            judgment_count=judgments,
            unknown_tags=unknown_tags,
            completeness=completeness,
            undelivered=tuple(undelivered),
            catalog_gaps=tuple(catalog_gaps),
        )

    # -- applying the edit to the base spec -------------------------------------------- #

    def _apply_edit(
        self,
        *,
        base_spec: DiagramSpec,
        delta_entities: list[PlannedEntity],
        delta_connections: list[PlannedConnection],
        remove_clauses: list[Clause],
        unknown_clauses: list[Clause],
        unknown_tags: tuple[str, ...],
        notes: list[str],
        skipped: list[str],
    ):
        undelivered: list[str] = [f"无法理解的子句：「{clause.text}」" for clause in unknown_clauses]

        removed_ids: set[str] = set()
        for clause in remove_clauses:
            named = [tag for tag in extract_tags(clause.text) if tag]
            if not named:
                undelivered.append(f"「{clause.text}」没有点明要删除的位号。")
                continue
            for tag in named:
                match = next(
                    (e for e in base_spec.entities if e.tag == tag),
                    None,
                )
                if match is None:
                    undelivered.append(f"「{clause.text}」：位号 {tag} 不在当前图纸里。")
                else:
                    removed_ids.add(match.engineering_id)

        kept_entities = [
            entity for entity in base_spec.entities if entity.engineering_id not in removed_ids
        ]
        dropped_base_connections = [
            connection
            for connection in base_spec.connections
            if connection.source_engineering_id in removed_ids
            or connection.target_engineering_id in removed_ids
        ]
        for connection in dropped_base_connections:
            notes.append(
                f"连接 {connection.engineering_id} 因端点被删除而移除（随删除生效，非未兑现）。"
            )
        kept_connections = [
            connection
            for connection in base_spec.connections
            if connection not in dropped_base_connections
        ]

        additions = [entity for entity in delta_entities if entity.resolved]
        duplicate_tags = {
            entity.tag
            for entity in additions
            if any(existing.tag == entity.tag for existing in kept_entities)
        }
        for tag in sorted(duplicate_tags):
            undelivered.append(f"「{additions[[e.tag for e in additions].index(tag)].phrase}」（位号 {tag}）：图纸里已有这个位号。")
            skipped.append(f"位号 {tag} 已存在，未重复添加。")
        system_id = base_spec.entities[0].system_id if base_spec.entities else self.system_id
        new_entities = [
            DiagramEntity(
                engineering_id=entity.engineering_id,
                kind="equipment" if _is_equipment(entity.chosen_symbol_key, self.symbols) else "instrument",
                system_id=system_id,
                tag=entity.tag,
                name=entity.phrase,
                equipment_class=self._category(entity.chosen_symbol_key),
                symbol_key=entity.chosen_symbol_key,
            )
            for entity in additions
            if entity.tag not in duplicate_tags
        ]

        new_connection_ids = {c.engineering_id for c in kept_connections}
        new_connections = list(kept_connections)
        for connection in delta_connections:
            if connection.chosen is None:
                continue
            source, target = connection.chosen
            if source in removed_ids or target in removed_ids:
                undelivered.append(f"「{connection.phrase}」没有被兑现。")
                continue
            declared = {e.engineering_id for e in kept_entities} | {e.engineering_id for e in new_entities}
            if source not in declared or target not in declared:
                undelivered.append(f"「{connection.phrase}」没有被兑现。")
                continue
            connection_id = f"cn_{len(new_connection_ids) + 1}"
            while connection_id in new_connection_ids:
                connection_id = f"cn_{int(connection_id[3:]) + 1}"
            new_connection_ids.add(connection_id)
            new_connections.append(
                DiagramConnection(
                    engineering_id=connection_id,
                    source_engineering_id=source,
                    target_engineering_id=target,
                    medium="",
                    tag="",
                )
            )
        for connection in delta_connections:
            if connection.chosen is None:
                undelivered.append(f"「{connection.phrase}」没有被兑现。")

        spec = base_spec.model_copy(
            update={"entities": [*kept_entities, *new_entities], "connections": new_connections}
        )

        # Catalogue-gap records for unresolved delta entities, same schema as the planner.
        catalog_gaps: list[dict] = []
        delivered = {entity.engineering_id for entity in spec.entities}
        for entity in delta_entities:
            if entity.engineering_id in delivered:
                continue
            if entity.catalog_gap:
                undelivered.append(f"「{entity.phrase}」（位号 {entity.tag}）：目录里没有这类设备的符号。")
                catalog_gaps.append(
                    {
                        "requested_type": entity.phrase,
                        "requested_tag": entity.tag,
                        "source_requirement": entity.source_clause,
                        "available_alternatives": [
                            {"key": row.key, "name": row.name, "category": row.category}
                            for row in available_alternatives(self.symbols)
                        ],
                    }
                )
            elif entity.tag in duplicate_tags:
                pass  # duplicate-tag receipt already recorded above
            else:
                undelivered.append(f"「{entity.phrase}」（位号 {entity.tag}）没有被兑现。")
        if unknown_tags:
            skipped.append(f"连接短语提到的位号 {list(unknown_tags)} 没有任何设备声明，已跳过。")
        return spec, undelivered, catalog_gaps

    def _category(self, symbol_key: str) -> str:
        try:
            return self.symbols.get(symbol_key).category
        except Exception:  # pragma: no cover - a candidate always came from the registry
            return ""

    def _incoherent(self, problems: list[str]):
        from .m7_text_planner import DiagramSpecPlanningError

        return DiagramSpecPlanningError(
            "typesafe_spec_incoherent",
            "editor produced an incoherent specification: " + "; ".join(problems),
            status_code=500,
        )


__all__ = ["TypesafeSpecEditor"]
