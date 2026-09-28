"""Q2R3-B1: the governed attachment-target resolver (pure, no I/O, no registry writes).

The resolver answers one question: an instrument that is semantically attached to a
host (``DiagramEntity.host_engineering_id``) may tap the host *where*, under the
frozen contract:

* only ports of the governed attachment class may serve as a tap -- port ids in the
  ``tap_`` namespace, classified here so the rule survives catalogue edits;
* ordinary process in/out ports never enter the candidate set, whatever their side
  or direction;
* zero candidates -> ``instrument_attachment_ambiguity / no_governed_tap_port``;
* more than one candidate -> ``instrument_attachment_ambiguity /
  multiple_governed_tap_ports`` (never nearest/first-match/side guessing);
* a resolved target identity is the port id -- a declared identity. Coordinates are
  derived later, after placement, through the single port-mapping formula, and never
  flow back into the semantic layer.

With the current built-in catalogue (no ``tap_`` ports anywhere) every real host
resolves to the no-governed-tap receipt, which is the honest terminal state until the
B2 catalogue work lands.
"""

from __future__ import annotations

from dataclasses import dataclass

AMBIGUITY_CODE = "instrument_attachment_ambiguity"
REASON_NO_GOVERNED_TAP_PORT = "no_governed_tap_port"
REASON_MULTIPLE_GOVERNED_TAP_PORTS = "multiple_governed_tap_ports"

#: The governed attachment-port class: a port id in this namespace is an
#: instrumentation tap nozzle, never a process endpoint. Kept as code (like the
#: phrase hint table) so the classification rule has one home.
_TAP_PORT_PREFIX = "tap_"

#: Deterministic instrument-type -> tap-port selection. An instrument type outside
#: this table has no governed tap and always receipts -- never falls back to a
#: process port. Keys are matched as substrings of the instrument symbol key.
_TAP_SELECTION: tuple[tuple[str, str], ...] = (
    ("level", "tap_level"),
    ("pressure", "tap_pt"),
    ("temperature", "tap_pt"),
)


@dataclass(frozen=True)
class AttachmentResolution:
    """The resolver's answer for one instrument/host pair."""

    instrument_tag: str
    host_tag: str
    host_symbol_key: str
    resolved_port_id: str | None
    ambiguity_code: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.resolved_port_id is not None

    def receipt(self) -> dict:
        """The machine-readable gap record the completeness ledger carries."""

        return {
            "code": self.ambiguity_code or AMBIGUITY_CODE,
            "reason": self.reason,
            "instrument_tag": self.instrument_tag,
            "host_tag": self.host_tag,
            "host_symbol_key": self.host_symbol_key,
            "resolved_port_id": self.resolved_port_id,
            "candidates": list(self.candidates),
        }


def _instrument_tap_port_id(instrument_symbol_key: str) -> str:
    key = (instrument_symbol_key or "").lower()
    for marker, tap_port in _TAP_SELECTION:
        if marker in key:
            return tap_port
    return ""


def resolve_attachment_target(
    *,
    instrument_tag: str,
    instrument_symbol_key: str,
    host_tag: str,
    host_symbol_key: str,
    host_port_ids: tuple[str, ...],
) -> AttachmentResolution:
    """Resolve the governed tap port for an attached instrument.

    ``host_port_ids`` is the declared port-id list of the host symbol -- the caller
    reads it from the frozen catalogue fact; this module never touches files.
    """

    governed = tuple(p for p in host_port_ids if p.startswith(_TAP_PORT_PREFIX))
    wanted = _instrument_tap_port_id(instrument_symbol_key)
    base = {
        "instrument_tag": instrument_tag,
        "host_tag": host_tag,
        "host_symbol_key": host_symbol_key,
    }
    if not governed:
        return AttachmentResolution(
            **base,
            resolved_port_id=None,
            ambiguity_code=AMBIGUITY_CODE,
            reason=REASON_NO_GOVERNED_TAP_PORT,
        )
    candidates = tuple(p for p in governed if not wanted or p == wanted)
    if len(candidates) != 1:
        return AttachmentResolution(
            **base,
            resolved_port_id=None,
            ambiguity_code=AMBIGUITY_CODE,
            reason=REASON_MULTIPLE_GOVERNED_TAP_PORTS,
            candidates=candidates or governed,
        )
    return AttachmentResolution(**base, resolved_port_id=candidates[0])
