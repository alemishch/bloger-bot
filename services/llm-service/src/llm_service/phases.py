"""Load per-blogger dialogue phase definitions (YAML) and build prompt snippets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

from llm_service.config import settings

logger = structlog.get_logger()

DEFAULT_SINGLE_PHASE_ID = "free_chat"


@dataclass(frozen=True)
class PhaseDef:
    id: str
    title: str
    typical_duration_days_min: int
    focus: str
    exit_signals: str
    prompt_injection: str


def _phases_dir() -> Path:
    raw = os.getenv("DIALOGUE_PHASES_DIR", "").strip()
    if raw:
        return Path(raw)
    # Sibling of bloggers dir: .../config/bloggers -> .../config/dialogue_phases
    return Path(settings.CONFIG_DIR).parent / "dialogue_phases"


def load_phase_config(blogger_id: str) -> tuple[list[PhaseDef], list[str]]:
    """
    Returns (phases, ordered_ids). If YAML missing, single synthetic phase free_chat (min_days 0).
    """
    path = _phases_dir() / f"{blogger_id}.yaml"
    if not path.exists():
        logger.warning("dialogue_phases_file_missing", path=str(path), blogger=blogger_id)
        return (
            [
                PhaseDef(
                    id=DEFAULT_SINGLE_PHASE_ID,
                    title="Свободный диалог",
                    typical_duration_days_min=0,
                    focus="Общий разбор по методологии эксперта.",
                    exit_signals="—",
                    prompt_injection="",
                )
            ],
            [DEFAULT_SINGLE_PHASE_ID],
        )

    raw = path.read_text(encoding="utf-8")
    expanded = os.path.expandvars(raw)
    data = yaml.safe_load(expanded) or {}
    rows = data.get("phases") or []
    phases: list[PhaseDef] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id") or "").strip()
        if not pid:
            continue
        phases.append(
            PhaseDef(
                id=pid,
                title=str(row.get("title") or pid),
                typical_duration_days_min=max(0, int(row.get("typical_duration_days_min", 3))),
                focus=str(row.get("focus") or "").strip(),
                exit_signals=str(row.get("exit_signals") or "").strip(),
                prompt_injection=str(row.get("prompt_injection") or "").strip(),
            )
        )
    if not phases:
        logger.warning("dialogue_phases_empty", path=str(path))
        return (
            [
                PhaseDef(
                    id=DEFAULT_SINGLE_PHASE_ID,
                    title="Свободный диалог",
                    typical_duration_days_min=0,
                    focus="Общий разбор по методологии эксперта.",
                    exit_signals="—",
                    prompt_injection="",
                )
            ],
            [DEFAULT_SINGLE_PHASE_ID],
        )

    return phases, [p.id for p in phases]


def get_phase_by_id(phases: list[PhaseDef], phase_id: str | None) -> PhaseDef | None:
    if not phase_id:
        return None
    for p in phases:
        if p.id == phase_id:
            return p
    return None


def default_phase_id(ordered_ids: list[str]) -> str:
    return ordered_ids[0] if ordered_ids else DEFAULT_SINGLE_PHASE_ID


def next_phase_id(ordered_ids: list[str], current_id: str) -> str | None:
    try:
        i = ordered_ids.index(current_id)
    except ValueError:
        return ordered_ids[1] if len(ordered_ids) > 1 else None
    if i + 1 < len(ordered_ids):
        return ordered_ids[i + 1]
    return None


def prev_phase_id(ordered_ids: list[str], current_id: str) -> str | None:
    try:
        i = ordered_ids.index(current_id)
    except ValueError:
        return None
    if i > 0:
        return ordered_ids[i - 1]
    return None


def phase_catalog_for_prompt(phases: list[PhaseDef]) -> str:
    """Compact list of ids and titles for the analysis JSON schema."""
    lines = []
    for i, p in enumerate(phases):
        lines.append(f"{i + 1}) {p.id} — {p.title} (min_days в фазе: {p.typical_duration_days_min})")
    return "\n".join(lines) if lines else "(нет фаз)"


def build_phase_context_block(
    *,
    phases: list[PhaseDef],
    ordered_ids: list[str],
    active: PhaseDef,
    days_in_phase: int,
    next_title: str | None,
) -> str:
    nxt = f"\nСледующая фаза (после завершения текущей): {next_title}" if next_title else ""
    return (
        f"АКТИВНАЯ ФАЗА ДИАЛОГА: {active.id} — {active.title}\n"
        f"Дней в этой фазе (календарных UTC, с phase_started_at): {days_in_phase}\n"
        f"Фокус этапа:\n{active.focus}\n\n"
        f"Сигналы выхода (для твоей оценки, не для пользователя дословно):\n{active.exit_signals}\n"
        f"{nxt}\n\n"
        f"Инструкция стиля этапа:\n{active.prompt_injection}\n"
    )


def append_phase_log(
    existing: Any,
    *,
    from_phase: str,
    to_phase: str,
    reason: str,
    max_entries: int = 10,
) -> list[dict[str, str]]:
    log: list[dict[str, str]] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                log.append({str(k): str(v)[:500] for k, v in item.items()})
    log.append(
        {
            "from": from_phase[:80],
            "to": to_phase[:80],
            "reason": reason[:500],
        }
    )
    return log[-max_entries:]
