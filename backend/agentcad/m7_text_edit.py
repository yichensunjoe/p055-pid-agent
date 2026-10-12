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

from dataclasses import replace

from .device_phrases import (
    Clause,
    available_alternatives,
    candidate_symbols,
    extract_tags,
    matched_hints,
    replace_target,
    split_clauses,
)
from .m7_diagram_spec import DiagramConnection, DiagramEntity, DiagramSpec
from .m7_synthesis_contract import Completeness
from .m7_text_planner import (
    PlannedConnection,
    PlannedDiagram,
    PlannedEntity,
    TypesafeDiagramSpecPlanner,
    _device_phrase,
    _is_equipment,
    attach_port_selectors,
)


class TypesafeSpecEditor(TypesafeDiagramSpecPlanner):
    """Plans an edit against a stored spec, reusing the planner's candidate discipline."""

    def plan_edit(self, prompt: str, *, base_spec: DiagramSpec, typesafe_config) -> PlannedDiagram:
        connect_clauses = [c for c in split_clauses(prompt) if c.kind == "connect"]
        remove_clauses = [c for c in split_clauses(prompt) if c.kind == "remove"]
        replace_clauses = [c for c in split_clauses(prompt) if c.kind == "replace"]
        unknown_clauses = [c for c in split_clauses(prompt) if c.kind == "unknown"]

        # Read only the *delta* devices the edit sentence declares; the base spec's
        # entities join the candidate pool as already-resolved facts.
        delta_entities, _base_connects, _sentence_unknown, _unknown = self.read(
            prompt,
            existing_tags=tuple(entity.tag for entity in base_spec.entities),
        )

        # DI-1-D4 F-e: 「把 <TAG> 换成 …」 replacement clauses. The target is data (the
        # tag the clause spells out, looked up in the base spec -- never inferred); the
        # replacement device kind is the only judgment, chosen among code-built candidates.
        # Replacement intents stay in their own list: a duplicate add mints the same
        # el_<tag> id as the entity it duplicates, so membership in the base spec must
        # never be what tells a replacement from an addition.
        replace_receipts: list[str] = []
        replacement_entities: list[PlannedEntity] = []
        for clause in replace_clauses:
            parsed = replace_target(clause.text)
            if parsed is None:
                replace_receipts.append(
                    f"「{clause.text}」不是可支持的替换子句（应为 把<位号>换成<新设备>）。"
                )
                continue
            target_tag, rest = parsed
            existing = next(
                (entity for entity in base_spec.entities if entity.tag == target_tag), None
            )
            if existing is None:
                replace_receipts.append(
                    f"「{clause.text}」：位号 {target_tag} 不在当前图纸里，替换未执行。"
                )
                continue
            if extract_tags(rest):
                replace_receipts.append(
                    f"「{clause.text}」：暂不支持替换并改名，替换未执行。"
                )
                continue
            phrase = _device_phrase(rest) or rest
            candidates = tuple(candidate_symbols(self.symbols, clause.text))
            replacement_entities.append(
                PlannedEntity(
                    engineering_id=existing.engineering_id,
                    tag=existing.tag,
                    phrase=phrase,
                    candidates=candidates,
                    decided_by="judgment",
                    source_clause=clause.text,
                    catalog_gap=bool(matched_hints(clause.text)) and not candidates,
                )
            )
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
        state, questions = self.questions(
            [*delta_entities, *replacement_entities], connections, label_entities=pool
        )
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
        replacement_entities = [
            self._resolve_entity(entity, answers.get(entity.engineering_id), notes, skipped)
            for entity in replacement_entities
        ]
        resolved_pool = {entity.engineering_id: entity for entity in [*base_entities, *delta_entities]}
        labels = {eid: entity.tag for eid, entity in resolved_pool.items()}
        connections = [
            self._resolve_connection(
                connection, answers.get(connection.engineering_id), labels, notes, skipped
            )
            for connection in connections
        ]
        connections = attach_port_selectors(connections, labels)

        spec, undelivered, catalog_gaps, connection_id_remap = self._apply_edit(
            base_spec=base_spec,
            delta_entities=delta_entities,
            delta_connections=connections,
            remove_clauses=remove_clauses,
            unknown_clauses=unknown_clauses,
            unknown_tags=unknown_tags,
            notes=notes,
            skipped=skipped,
            replace_entities=replacement_entities,
            replace_receipts=replace_receipts,
        )
        problems = spec.problems()
        if problems:
            raise self._incoherent(problems)

        # The merged spec remints connection ids so they never collide with the base's
        # (delta cn_1/cn_2 become cn_2/cn_3 when the base already holds a cn_1). The
        # selector map downstream is keyed by the *planner's* connection ids, so the
        # planned connections must carry the remapped ids -- otherwise the first new
        # connection silently inherits the second connection's selectors.
        connections = [
            replace(connection, engineering_id=connection_id_remap.get(
                connection.engineering_id, connection.engineering_id
            ))
            if connection.chosen is not None and connection.engineering_id in connection_id_remap
            else connection
            for connection in connections
        ]

        resolved_count = len(spec.entities)
        attachment_gaps, attachment_receipts = self._attachment_ledger(spec)
        undelivered.extend(attachment_receipts)
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
            attachment_gaps=attachment_gaps,
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
        replace_entities: list[PlannedEntity] | None = None,
        replace_receipts: list[str] | None = None,
    ):
        undelivered: list[str] = [f"无法理解的子句：「{clause.text}」" for clause in unknown_clauses]
        undelivered.extend(replace_receipts or [])

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

        # Q2R3-B1 host-deletion guard, evaluated on the semantic final state before the
        # removal set takes effect: a host with surviving attached instruments is NOT
        # removed. No cascade delete, no silent detach, no rehost -- the delete clause
        # receipts with the machine code and named dependents. Deleting every dependent
        # instrument together with the host in one request stays allowed.
        blocked_hosts: dict[str, list[str]] = {}
        for entity in base_spec.entities:
            if (
                entity.engineering_id not in removed_ids
                and entity.kind == "instrument"
                and entity.host_engineering_id
                and entity.host_engineering_id in removed_ids
            ):
                blocked_hosts.setdefault(entity.host_engineering_id, []).append(entity.tag)
        for host_id, dependent_tags in sorted(blocked_hosts.items()):
            removed_ids.discard(host_id)
            host_tag = next(
                (entity.tag for entity in base_spec.entities if entity.engineering_id == host_id),
                host_id,
            )
            undelivered.append(
                f"attachment_host_has_dependents：宿主 {host_tag}（{host_id}）仍挂有仪表 "
                f"{list(dependent_tags)}，删除未执行。"
            )

        kept_entities = [
            entity for entity in base_spec.entities if entity.engineering_id not in removed_ids
        ]
        # F-e replacements: a resolved delta entity whose id already exists in the base is
        # a replacement, not an addition — swap the symbol in place (position in the entity
        # order, tag, host and every connection are kept), never duplicate-tag receipt it.
        replacement_by_id = {
            entity.engineering_id: entity
            for entity in (replace_entities or [])
            if entity.resolved and entity.engineering_id in {e.engineering_id for e in kept_entities}
        }
        if replacement_by_id:
            kept_entities = [
                self._replaced_entity(entity, replacement_by_id[entity.engineering_id])
                if entity.engineering_id in replacement_by_id
                else entity
                for entity in kept_entities
            ]
            for entity_id in sorted(replacement_by_id):
                notes.append(
                    f"位号 {replacement_by_id[entity_id].tag} 已替换为 "
                    f"{replacement_by_id[entity_id].phrase}（连接与身份保持不变）。"
                )
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

        additions = [
            entity
            for entity in delta_entities
            if entity.resolved
        ]
        duplicate_tags = {
            entity.tag
            for entity in additions
            if any(existing.tag == entity.tag for existing in kept_entities)
        }
        for tag in sorted(duplicate_tags):
            undelivered.append(f"「{additions[[e.tag for e in additions].index(tag)].phrase}」（位号 {tag}）：图纸里已有这个位号。")
            skipped.append(f"位号 {tag} 已存在，未重复添加。")
        system_id = base_spec.entities[0].system_id if base_spec.entities else self.system_id
        new_entities: list[DiagramEntity] = []
        for entity in additions:
            if entity.tag in duplicate_tags:
                continue
            instrument = not _is_equipment(entity.chosen_symbol_key, self.symbols)
            host: str | None = None
            if instrument and entity.host_tag:
                pool_labels = {
                    e.tag: e.engineering_id
                    for e in [*kept_entities, *new_entities]
                }
                host = pool_labels.get(entity.host_tag)
                if host is None:
                    undelivered.append(
                        f"「{entity.source_clause or entity.phrase}」的宿主 {entity.host_tag} "
                        "未声明，仪表挂接未建立。"
                    )
            new_entities.append(
                DiagramEntity(
                    engineering_id=entity.engineering_id,
                    kind="equipment" if _is_equipment(entity.chosen_symbol_key, self.symbols) else "instrument",
                    system_id=system_id,
                    tag=entity.tag,
                    name=entity.phrase,
                    equipment_class=self._category(entity.chosen_symbol_key),
                    symbol_key=entity.chosen_symbol_key,
                    host_engineering_id=host,
                )
            )

        new_connection_ids = {c.engineering_id for c in kept_connections}
        new_connections = list(kept_connections)
        connection_id_remap: dict[str, str] = {}
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
            # The selector map is keyed by the planner's clause id; record where this
            # clause's connection landed in the merged spec so the caller can rekey it.
            connection_id_remap[connection.engineering_id] = connection_id
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
        return spec, undelivered, catalog_gaps, connection_id_remap

    def _category(self, symbol_key: str) -> str:
        try:
            return self.symbols.get(symbol_key).category
        except Exception:  # pragma: no cover - a candidate always came from the registry
            return ""

    def _replaced_entity(self, base: DiagramEntity, planned: PlannedEntity) -> DiagramEntity:
        """F-e: the base entity after an in-place replacement -- identity, tag, position in
        the entity order and every connection survive; the symbol and its derived kind
        fields take the replacement. A flip to equipment drops the instrument's host
        (equipment never carries one); a flip to instrument clears the stale class."""

        instrument = not _is_equipment(planned.chosen_symbol_key, self.symbols)
        return base.model_copy(
            update={
                "kind": "instrument" if instrument else "equipment",
                "name": planned.phrase,
                "symbol_key": planned.chosen_symbol_key,
                "equipment_class": self._category(planned.chosen_symbol_key),
                "instrument_type": base.instrument_type if instrument else "",
                "host_engineering_id": base.host_engineering_id if instrument else None,
            }
        )

    def _incoherent(self, problems: list[str]):
        from .m7_text_planner import DiagramSpecPlanningError

        return DiagramSpecPlanningError(
            "typesafe_spec_incoherent",
            "editor produced an incoherent specification: " + "; ".join(problems),
            status_code=500,
        )


__all__ = ["TypesafeSpecEditor"]
