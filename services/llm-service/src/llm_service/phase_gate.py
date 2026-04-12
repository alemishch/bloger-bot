"""Calendar and ordering rules for dialogue phase transitions (pure logic, testable)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from llm_service.phases import PhaseDef, default_phase_id


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _fallback_start(ensure_start_date: str | None) -> str:
    if ensure_start_date and str(ensure_start_date).strip():
        return str(ensure_start_date).strip()[:10]
    return utc_today().isoformat()


def days_since_phase_start(phase_started_at: str | None) -> int | None:
    """
    Calendar days since phase_started_at (UTC date). None if start date not yet set (unknown).
    """
    if not phase_started_at or not str(phase_started_at).strip():
        return None
    try:
        d = date.fromisoformat(str(phase_started_at).strip()[:10])
    except ValueError:
        return None
    delta = utc_today() - d
    return max(0, delta.days)


def index_of(ordered_ids: list[str], phase_id: str) -> int:
    try:
        return ordered_ids.index(phase_id)
    except ValueError:
        return -1


def resolve_phase_transition(
    *,
    phases: list[PhaseDef],
    ordered_ids: list[str],
    profile_phase_id: str | None,
    profile_phase_started_at: str | None,
    transition: dict | None,
    ensure_start_date: str | None,
) -> tuple[str, str, dict[str, str], dict]:
    """
    Decide effective phase and profile updates.

    ensure_start_date: ISO date (YYYY-MM-DD) to set phase_started_at when missing (first turn).

    Returns:
      (effective_phase_id, phase_started_at_iso, profile_delta, debug_info)
    """
    debug: dict = {"gate_notes": []}
    if not ordered_ids:
        ordered_ids = [default_phase_id([])]

    current_id = (profile_phase_id or "").strip() or default_phase_id(ordered_ids)
    if current_id not in ordered_ids:
        current_id = default_phase_id(ordered_ids)
        debug["gate_notes"].append("unknown_profile_phase_reset_to_default")

    started_at = (profile_phase_started_at or "").strip() or None
    delta: dict = {}

    # First-time: anchor calendar for min-days rule
    if not started_at and ensure_start_date:
        started_at = ensure_start_date[:10]
        delta["phase_started_at"] = started_at
        delta["dialogue_phase"] = current_id
        debug["gate_notes"].append("initialized_phase_started_at")

    days = days_since_phase_start(started_at)
    if days is None:
        days = 0

    current_def = next((p for p in phases if p.id == current_id), None)
    min_days = current_def.typical_duration_days_min if current_def else 0

    tr = transition if isinstance(transition, dict) else {}
    requested = bool(tr.get("requested"))
    to_raw = tr.get("to_phase_id")
    to_id = str(to_raw).strip() if to_raw else None
    reason = str(tr.get("reason") or "").strip()[:500]

    if not requested or not to_id:
        debug["gate_notes"].append("no_transition_request")
        return current_id, started_at or _fallback_start(ensure_start_date), delta, debug

    if to_id not in ordered_ids:
        debug["gate_notes"].append("reject_unknown_target_phase")
        return current_id, started_at or _fallback_start(ensure_start_date), delta, debug

    ci = index_of(ordered_ids, current_id)
    ti = index_of(ordered_ids, to_id)

    if ti == ci:
        debug["gate_notes"].append("no_op_same_phase")
        return current_id, started_at or _fallback_start(ensure_start_date), delta, debug

    # Backward: allowed without day gate
    if ti < ci:
        debug["gate_notes"].append("backward_transition_allowed")
        delta["dialogue_phase"] = to_id
        delta["phase_started_at"] = _fallback_start(ensure_start_date)
        return to_id, delta["phase_started_at"], delta, debug

    # Forward
    if ti > ci + 1:
        debug["gate_notes"].append("reject_skip_forward")
        return current_id, started_at or _fallback_start(ensure_start_date), delta, debug

    if ti == ci + 1:
        if min_days > 0 and days < min_days:
            debug["gate_notes"].append(f"reject_forward_min_days_not_met need={min_days} have={days}")
            return current_id, started_at or _fallback_start(ensure_start_date), delta, debug
        # First phase in the ladder: forward only if the model affirms ~"enough data" (≈3 psychologist visits).
        if ci == 0 and not bool(tr.get("sufficient_understanding")):
            debug["gate_notes"].append("reject_first_phase_insufficient_understanding")
            return current_id, started_at or _fallback_start(ensure_start_date), delta, debug
        debug["gate_notes"].append("forward_transition_allowed")
        delta["dialogue_phase"] = to_id
        delta["phase_started_at"] = _fallback_start(ensure_start_date)
        return to_id, delta["phase_started_at"], delta, debug

    return current_id, started_at or _fallback_start(ensure_start_date), delta, debug


def rejection_hint_for_hypothesis(notes: list[str]) -> str:
    if not notes:
        return ""
    if any("reject_first_phase_insufficient_understanding" in n for n in notes):
        return (
            "ПЕРВАЯ ФАЗА (сбор как у психолога): переход к «работе» с паттернами ещё не разрешён — по данным мало для ~3 визитов. "
            "Не веди себя как на этапе лечения: в основном вопросы и прояснение, без развёрнутых гипотез-назначений и без программ изменений."
        )
    if any("reject_forward_min_days" in n for n in notes):
        return (
            "КАЛЕНДАРНЫЙ ГЕЙТ ФАЗЫ: рано переходить к следующей фазе (минимум дней в текущей ещё не выдержан). "
            "Углуби текущую фазу: один новый угол, один точный вопрос или наблюдение — без смены этапа."
        )
    if any("reject_skip_forward" in n for n in notes):
        return (
            "Переход через фазу запрещён: двигайся только на следующую фазу по порядку или оставайся в текущей."
        )
    return ""
