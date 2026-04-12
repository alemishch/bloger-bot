"""Unit tests for calendar phase gating (no API calls)."""

from __future__ import annotations

from datetime import timedelta

from llm_service.phase_gate import resolve_phase_transition, utc_today
from llm_service.phases import PhaseDef


def _two_phases():
    return (
        [
            PhaseDef("alpha", "A", 3, "", "", ""),
            PhaseDef("beta", "B", 3, "", "", ""),
        ],
        ["alpha", "beta"],
    )


def test_forward_blocked_until_min_days():
    phases, ordered = _two_phases()
    eff, _, delta, dbg = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered,
        profile_phase_id="alpha",
        profile_phase_started_at=utc_today().isoformat(),
        transition={"requested": True, "to_phase_id": "beta", "reason": "test"},
        ensure_start_date=utc_today().isoformat(),
    )
    assert eff == "alpha"
    assert delta.get("dialogue_phase") != "beta"
    assert any("reject_forward_min_days" in str(x) for x in dbg.get("gate_notes", []))


def test_first_phase_blocked_without_sufficient_understanding():
    phases, ordered = _two_phases()
    old = (utc_today() - timedelta(days=10)).isoformat()
    eff, _, delta, dbg = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered,
        profile_phase_id="alpha",
        profile_phase_started_at=old,
        transition={
            "requested": True,
            "to_phase_id": "beta",
            "sufficient_understanding": False,
        },
        ensure_start_date=utc_today().isoformat(),
    )
    assert eff == "alpha"
    assert delta.get("dialogue_phase") != "beta"
    assert any("reject_first_phase_insufficient_understanding" in str(x) for x in dbg.get("gate_notes", []))


def test_forward_allowed_after_min_days():
    phases, ordered = _two_phases()
    old = (utc_today() - timedelta(days=4)).isoformat()
    eff, started, delta, dbg = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered,
        profile_phase_id="alpha",
        profile_phase_started_at=old,
        transition={
            "requested": True,
            "to_phase_id": "beta",
            "reason": "test",
            "sufficient_understanding": True,
        },
        ensure_start_date=utc_today().isoformat(),
    )
    assert eff == "beta"
    assert delta.get("dialogue_phase") == "beta"
    assert started == utc_today().isoformat()[:10]
    assert any("forward_transition_allowed" in str(x) for x in dbg.get("gate_notes", []))


def test_backward_allowed_without_day_gate():
    phases, ordered = _two_phases()
    eff, _, delta, dbg = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered,
        profile_phase_id="beta",
        profile_phase_started_at=utc_today().isoformat(),
        transition={"requested": True, "to_phase_id": "alpha", "reason": "test"},
        ensure_start_date=utc_today().isoformat(),
    )
    assert eff == "alpha"
    assert delta.get("dialogue_phase") == "alpha"
    assert any("backward" in str(x).lower() for x in dbg.get("gate_notes", []))


def test_skip_forward_rejected():
    phases, ordered = (
        [
            PhaseDef("a", "", 0, "", "", ""),
            PhaseDef("b", "", 0, "", "", ""),
            PhaseDef("c", "", 0, "", "", ""),
        ],
        ["a", "b", "c"],
    )
    eff, _, delta, _ = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered,
        profile_phase_id="a",
        profile_phase_started_at=(utc_today() - timedelta(days=10)).isoformat(),
        transition={"requested": True, "to_phase_id": "c", "sufficient_understanding": True},
        ensure_start_date=utc_today().isoformat(),
    )
    assert eff == "a"
    assert "dialogue_phase" not in delta or delta.get("dialogue_phase") != "c"
