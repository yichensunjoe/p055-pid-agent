"""M11-D2: the Cable Schematic domain models (production slice).

Single source of truth lives in the cable_documents envelope (revision) plus
this JSON contract (payload); the registry is identity-only (M11-D1). The
payload never carries an authoritative revision — CableDocument.revision is
injected from the envelope at load time and must equal it (fail-closed).
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .runtime.primitives import StrictModel

CABLE_DOCUMENT_SCHEMA = "pid-agent.cable-document/1"


class CableSegment(StrictModel):
    id: str = Field(min_length=1)
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    gauge: str = "2.5mm2"

    @field_validator("to_node")
    @classmethod
    def _nodes_differ(cls, value: str, info) -> str:
        if "from_node" in info.data and value == info.data["from_node"]:
            raise ValueError("cable segment requires from_node != to_node")
        return value


class CableDocument(StrictModel):
    """Semantic payload of one cable schematic document.

    ``revision`` is derived: it is injected from the envelope on load and is
    never serialized into data_json as an authoritative fact.
    """

    schema: Literal["pid-agent.cable-document/1"]
    name: str = "cable schematic"
    segments: tuple[CableSegment, ...] = ()
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _segment_ids_unique(self) -> CableDocument:
        ids = [segment.id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate cable segment id in document payload")
        return self

    def segment_ids(self) -> set[str]:
        return {segment.id for segment in self.segments}

    def with_segment(self, segment: CableSegment) -> CableDocument:
        if segment.id in self.segment_ids():
            raise ValueError(f"duplicate cable segment id: {segment.id}")
        return self.model_copy(
            update={"segments": (*self.segments, segment), "revision": self.revision + 1}
        )


class AddCableSegmentIntent(StrictModel):
    """The governed Cable write intent. ``expected_revision`` is part of the
    approval identity (canonical intent) and is re-checked at execution."""

    expected_revision: int = Field(ge=0)
    segment: CableSegment


def parse_cable_payload(data_json: str, envelope_revision: int) -> CableDocument:
    document = CableDocument.model_validate_json(data_json)
    # The payload never carries an authoritative revision (A2): the envelope
    # is the single revision truth, injected at the IO boundary. If a payload
    # DOES contain a revision field (we strip it on write), it must agree with
    # the envelope — otherwise the payload was tampered with: fail closed.
    explicit = json.loads(data_json).get("revision") if data_json.strip().startswith("{") else None
    if explicit is not None and int(explicit) != envelope_revision:
        raise ValueError(
            "cable payload revision cache disagrees with the envelope "
            f"({explicit} != {envelope_revision})"
        )
    return document.model_copy(update={"revision": envelope_revision})


def serialize_cable_payload(document: CableDocument) -> str:
    # revision is a derived envelope fact: strip it from the payload so the
    # envelope stays the single revision truth.
    return document.model_dump_json(exclude={"revision"})
