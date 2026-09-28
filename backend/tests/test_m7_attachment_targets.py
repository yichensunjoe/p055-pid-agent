"""Q2R3-B1: the governed attachment-target resolver's contract.

The resolver is pure: the caller supplies the host symbol's declared port ids, and the
answer is a stable port id or a machine-readable ambiguity. Ordinary process ports
never enter the candidate set, and no rule ever guesses from geometry.
"""

from agentcad.m7_attachment_targets import (
    REASON_NO_GOVERNED_TAP_PORT,
    REASON_NO_MATCHING_GOVERNED_TAP_PORT,
    REASON_UNSUPPORTED_INSTRUMENT_TYPE,
    resolve_attachment_target,
)


def test_host_without_governed_tap_port_receipts_honestly() -> None:
    """The current built-in catalogue shape: only process ports exist."""

    result = resolve_attachment_target(
        instrument_tag="TT-101",
        instrument_symbol_key="temperature_transmitter",
        host_tag="V-101",
        host_symbol_key="buffer_tank",
        host_port_ids=("in", "out"),
    )
    assert not result.resolved
    assert result.reason == REASON_NO_GOVERNED_TAP_PORT
    assert result.receipt()["code"] == "instrument_attachment_ambiguity"
    assert result.receipt()["host_symbol_key"] == "buffer_tank"


def test_process_ports_never_become_tap_candidates() -> None:
    """Even beside a governed tap, ordinary in/out ports stay out of the candidate set."""

    result = resolve_attachment_target(
        instrument_tag="LIT-101",
        instrument_symbol_key="level_gauge",
        host_tag="V-101",
        host_symbol_key="buffer_tank",
        host_port_ids=("in", "out", "tap_level"),
    )
    assert result.resolved
    assert result.resolved_port_id == "tap_level"


def test_instrument_type_selects_its_governed_tap_deterministically() -> None:
    for instrument_key, expected in (
        ("level_gauge", "tap_level"),
        ("level_transmitter", "tap_level"),
        ("pressure_indicator", "tap_pt"),
        ("temperature_transmitter", "tap_pt"),
    ):
        result = resolve_attachment_target(
            instrument_tag="X-101",
            instrument_symbol_key=instrument_key,
            host_tag="V-101",
            host_symbol_key="buffer_tank",
            host_port_ids=("tap_level", "tap_pt"),
        )
        assert result.resolved, instrument_key
        assert result.resolved_port_id == expected, instrument_key


def test_unknown_instrument_type_never_guesses_even_with_a_single_tap() -> None:
    """The fail-closed hole: an unmapped instrument type must receipt even when the host
    has exactly one governed port -- 'the only tap' is never an implicit answer."""

    result = resolve_attachment_target(
        instrument_tag="FT-101",
        instrument_symbol_key="flow_transmitter",
        host_tag="V-101",
        host_symbol_key="buffer_tank",
        host_port_ids=("tap_pt",),
    )
    assert not result.resolved
    assert result.reason == REASON_UNSUPPORTED_INSTRUMENT_TYPE
    assert result.candidates == ("tap_pt",)


def test_known_type_without_its_tap_is_no_matching_not_multiple() -> None:
    """TT needs tap_pt; a host carrying only tap_level is a missing match, never a
    'multiple' receipt."""

    result = resolve_attachment_target(
        instrument_tag="TT-101",
        instrument_symbol_key="temperature_transmitter",
        host_tag="V-101",
        host_symbol_key="buffer_tank",
        host_port_ids=("in", "out", "tap_level"),
    )
    assert not result.resolved
    assert result.reason == REASON_NO_MATCHING_GOVERNED_TAP_PORT
    assert result.candidates == ("tap_level",)
