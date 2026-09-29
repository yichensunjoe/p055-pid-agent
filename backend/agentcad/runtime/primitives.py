"""Domain-neutral model primitives (M10-P1, Gate-frozen).

``StrictModel`` and ``utc_now`` are defined HERE and only here; the P&ID model
module (``agentcad.models``) and the harness model module re-export them, so
every existing ``from agentcad.models import StrictModel`` call site keeps
working unchanged. The frozen contract (M10-P1 hard locks) is class identity
plus unchanged validation / JSON-schema / serialization behaviour — not
byte-identical module metadata (``__module__`` legitimately changes).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every strict model in the system (domain-neutral since M10-P1)."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["StrictModel", "utc_now"]
