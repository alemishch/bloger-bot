"""Pack chat history and long-term profile into a compact brief for pipeline agents."""

from __future__ import annotations

import json
import re
from typing import Any


def _last_n_messages(history: list[dict] | None, max_turns: int = 6) -> list[dict]:
    if not history:
        return []
    # Count "turns" as pairs; we take last max_turns*2 messages max (user+assistant)
    cap = max_turns * 2
    return history[-cap:]


_OVERUSED_MARKERS: list[tuple[str, str]] = [
    ("скажем так", "вступление «Скажем так»"),
    ("обращали внимание", "блок «Обращали внимание…»"),
    ("давайте посмотрим", "вопрос «Давайте посмотрим…»"),
    ("удивительно но факт", "«Удивительно но факт»"),
    ("ближайший шаг", "строка «Ближайший шаг:»"),
    ("записывайте", "совет вести записи/дневник"),
    ("зафиксируйте", "совет «зафиксируйте»"),
    ("социальн", "акцент на «социальные связи / как часто общаетесь»"),
    ("глубокое дыхание", "техника глубокого дыхания"),
    ("пять чувств", "техника «пять чувств»"),
    ("заземлит", "советы «заземлиться»"),
]


def count_user_turns(chat_history: list[dict] | None) -> int:
    return sum(1 for m in (chat_history or []) if m.get("role") == "user")


def build_anti_repetition_block(
    chat_history: list[dict] | None,
    *,
    max_assistant_messages: int = 4,
) -> str:
    """
    Summarize recent assistant turns and flag overused openers / generic coach patterns
    so the next reply varies structure and deepens the thread.
    """
    msgs = chat_history or []
    assistants: list[str] = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").strip()
        if c:
            assistants.append(c)
    assistants = assistants[-max_assistant_messages:]
    if not assistants:
        return ""

    lines: list[str] = [
        "КОНТЕКСТ ПРОТИВ ШАБЛОНА (последние ответы ассистента в этой сессии):",
        "Не копируй их структуру, порядок абзацев и набор советов. Следующий ответ — продолжение дуги, не новый «урок с нуля».",
    ]
    for i, a in enumerate(assistants, 1):
        one_line = re.sub(r"\s+", " ", a)[:380]
        lines.append(f"{i}) …{one_line}…")

    lower_blocks = [a.lower() for a in assistants]
    avoid: list[str] = []
    for needle, label in _OVERUSED_MARKERS:
        hits = sum(1 for lb in lower_blocks if needle in lb)
        if hits >= 2:
            avoid.append(f"{label} (повторялось в {hits} из последних ответов — в этом сообщении без этого)")
        elif hits == 1 and len(assistants) >= 2 and needle in lower_blocks[-1]:
            avoid.append(f"{label} (было в прошлом ответе — не повторяй тот же приём)")

    if avoid:
        lines.append("")
        lines.append("В ЭТОМ ХОДУ ИЗБЕГАЙ:")
        lines.extend(f"- {x}" for x in avoid)

    lines.append("")
    lines.append(
        "Если пользователь уже отреагировал на прошлый шаг (попробовал, не вышло, новый симптом) — "
        "отвечай следующим слоем: гипотеза по методологии или один точный вопрос, а не снова общий опросник и список техник из интернет-коучинга."
    )
    return "\n".join(lines)


def pack_context(
    chat_history: list[dict] | None,
    user_profile: dict | None,
    dialogue_phase: str = "free_chat",
    working_turns: int = 6,
) -> dict[str, Any]:
    """
    Build a memory brief: working memory (recent turns) + profile slices for agents.

    Optional profile keys (filled by update_user_profile over time):
    - pattern_summary: str — recurring themes / automatisms
    - previous_hypotheses: list[dict] | str — last turn hypotheses for continuity
    - dialogue_phase can also live in profile and override the argument if present.
    """
    profile = user_profile if isinstance(user_profile, dict) else {}
    phase = profile.get("dialogue_phase") or dialogue_phase

    recent = _last_n_messages(chat_history, working_turns)
    lines: list[str] = []
    for m in recent:
        role = m.get("role", "?")
        content = (m.get("content") or "")[:1200]
        lines.append(f"[{role}]: {content}")

    working_block = "\n".join(lines) if lines else "(нет недавних реплик)"

    profile_bits: list[str] = []
    for key in (
        "name",
        "goals",
        "communication_style",
        "topics_of_interest",
        "last_session_summary",
        "pattern_summary",
    ):
        val = profile.get(key)
        if val:
            if isinstance(val, (list, dict)):
                profile_bits.append(f"{key}: {json.dumps(val, ensure_ascii=False)[:800]}")
            else:
                profile_bits.append(f"{key}: {str(val)[:800]}")

    prev_hyp = profile.get("previous_hypotheses")
    if prev_hyp:
        if isinstance(prev_hyp, list):
            profile_bits.append(
                "previous_hypotheses: " + json.dumps(prev_hyp, ensure_ascii=False)[:1500]
            )
        else:
            profile_bits.append(f"previous_hypotheses: {str(prev_hyp)[:1500]}")

    profile_block = "\n".join(profile_bits) if profile_bits else "(профиль пуст или минимален)"

    return {
        "dialogue_phase": phase,
        "working_memory_text": working_block,
        "profile_text": profile_block,
        "raw_profile": profile,
    }
