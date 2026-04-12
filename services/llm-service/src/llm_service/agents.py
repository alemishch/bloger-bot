"""Pipeline agents: analysis, hypothesis, rerank, generation, quality judge."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from openai import APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from llm_service.retrieval import format_ref

logger = structlog.get_logger()


def _sanitize_api_text(text: str | None, max_chars: int) -> str:
    """Valid UTF-8, no NULs/surrogates; truncate for safe OpenAI JSON bodies."""
    if not text:
        return ""
    s = text.replace("\x00", "")
    s = "".join(ch for ch in s if not (0xD800 <= ord(ch) <= 0xDFFF))
    s = s.encode("utf-8", errors="replace").decode("utf-8")
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


def _safe_json_for_prompt(obj: Any, max_chars: int) -> str:
    """JSON for embedding in prompts; reject NaN/Inf that break strict JSON."""
    try:
        s = json.dumps(obj, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        s = json.dumps(str(obj), ensure_ascii=False)
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


def _add_usage(acc: dict[str, int], resp: ChatCompletion | None) -> None:
    if not resp or not resp.usage:
        return
    acc["prompt_tokens"] = acc.get("prompt_tokens", 0) + (resp.usage.prompt_tokens or 0)
    acc["completion_tokens"] = acc.get("completion_tokens", 0) + (resp.usage.completion_tokens or 0)


ANALYSIS_SCHEMA_HINT = """
Верни ТОЛЬКО JSON со ключами:
- intent: string (один из: запрос_причины, жалоба, уточнение, сопротивление, готовность_к_действию, small_talk, другое)
- entities: object с опциональными массивами/строками: симптомы, контекст, триггеры, отношения
- emotional_tone: string
- what_user_already_knows: array of strings (всё что пользователь уже явно понимает или сказал)
- what_user_resists: array of strings (сопротивления, защита позиции)
- information_gaps: array of strings (чего не хватает для качественной гипотезы)
- phase_readiness: object с полями enough_for_analysis: bool, enough_for_hypothesis: bool, missing_slots: array of strings
"""


async def run_analysis_agent(
    client: AsyncOpenAI,
    *,
    user_message: str,
    packed_working_memory: str,
    packed_profile: str,
    dialogue_phase: str,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    system = (
        "Ты — аналитический модуль системы. Извлеки структурированную информацию из сообщения пользователя. "
        "Ты НЕ генерируешь ответ пользователю.\n\n" + ANALYSIS_SCHEMA_HINT
    )
    um = _sanitize_api_text(user_message, 6000)
    user = (
        f"Текущая фаза диалога: {dialogue_phase}\n\n"
        f"Профиль и память (кратко):\n{packed_profile}\n\n"
        f"Недавний диалог:\n{packed_working_memory}\n\n"
        f"Новое сообщение пользователя:\n{um}\n\n"
        "Ответь строго JSON."
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )
    _add_usage(usage_acc, resp)
    stage_timings_ms["analysis"] = (time.perf_counter() - t0) * 1000
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("analysis_json_fallback")
        return {
            "intent": "другое",
            "entities": {},
            "emotional_tone": "",
            "what_user_already_knows": [],
            "what_user_resists": [],
            "information_gaps": [],
            "phase_readiness": {"enough_for_analysis": True, "enough_for_hypothesis": True, "missing_slots": []},
        }


HYPOTHESIS_SCHEMA_HINT = """
Верни ТОЛЬКО JSON:
- problem_zones: array of {zone: string, priority: number, evidence: string, pattern_detected: string}
- hypotheses: array of {id: string, zone: string, expert_role: string, hypothesis: string, confidence: number,
  evidence_from_user: string, evidence_from_methodology: string, content_to_retrieve: string, missing_info: string (опционально)}
- response_plan: object с полями:
  - strategy: string
  - key_insight: string
  - content_needed: array of strings
  - next_action: string
  - DO_NOT: string (чего генератору нельзя делать: пересказ, банальности, задания если запрещено и т.д.)
"""


async def run_hypothesis_agent(
    client: AsyncOpenAI,
    *,
    analysis: dict[str, Any],
    packed_profile: str,
    methodology_framework: str,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    user_turn_index: int = 1,
    anti_repeat_block: str = "",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    depth_rules = ""
    if user_turn_index > 1:
        depth_rules = (
            f"\nХОД ДИАЛОГА: это сообщение пользователя №{user_turn_index} в истории. "
            "next_action должен УГЛУБЛЯТЬ линию (новый угол методологии, один точный вопрос, уточнение гипотезы), "
            "а не заново выдавать тот же тип шага (дневник, опрос про соцсвязи, список дыхательных техник), "
            "если пользователь уже откликнулся на прошлый шаг или принёс новую деталь. "
            "В DO_NOT явно запрети повтор уже предложенных в недавних ответах бота инструментов и вопросов.\n"
        )
    anti = _sanitize_api_text(anti_repeat_block, 4500) if anti_repeat_block else ""
    anti_section = f"\nАНТИ-ПОВТОР (недавние ответы бота — план и DO_NOT должны это учитывать):\n{anti}\n" if anti else ""

    system = (
        "Ты — аналитический модуль, работающий СТРОГО из методологии эксперта ниже. "
        "Не опирайся на общие медицинские клише вне методологии. "
        "Ты НЕ пишешь ответ пользователю — только JSON.\n\n"
        f"МЕТОДОЛОГИЯ:\n{methodology_framework}\n\n"
        + HYPOTHESIS_SCHEMA_HINT
        + depth_rules
    )
    analysis_s = _safe_json_for_prompt(analysis, 6000)
    user = (
        f"Структурированный анализ сообщения:\n{analysis_s}\n\n"
        f"Профиль пользователя:\n{packed_profile}\n"
        f"{anti_section}"
        "Сформируй problem_zones, hypotheses (1-3), response_plan с явным DO_NOT."
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.35,
        max_tokens=2000,
        response_format={"type": "json_object"},
    )
    _add_usage(usage_acc, resp)
    stage_timings_ms["hypothesis"] = (time.perf_counter() - t0) * 1000
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("hypothesis_json_fallback")
        return {
            "problem_zones": [],
            "hypotheses": [],
            "response_plan": {
                "strategy": "",
                "key_insight": "",
                "content_needed": [],
                "next_action": "",
                "DO_NOT": "не пересказывать слова пользователя как открытие",
            },
        }


RERANK_SCHEMA = """
Для каждого фрагмента оцени 1-10:
- relevance: насколько фрагмент про ситуацию пользователя (не просто похожая тема)
- citation_value: насколько фрагмент даёт новый угол / кейс / формулировку методологии

Верни ТОЛЬКО JSON: {"rankings": [{"index": number, "relevance": number, "citation_value": number, "keep": boolean}]}
Отбрось keep=false или где min(relevance,citation_value) < 7.
"""


async def run_rerank_chunks(
    client: AsyncOpenAI,
    *,
    user_message: str,
    analysis: dict[str, Any],
    what_user_already_knows: list[str],
    candidates: list[dict[str, Any]],
    blogger_id: str,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    max_keep: int = 5,
) -> list[dict[str, Any]]:
    t0 = time.perf_counter()
    if not candidates:
        stage_timings_ms["rerank"] = (time.perf_counter() - t0) * 1000
        return []

    lines = []
    for i, c in enumerate(candidates[:20]):
        meta = c.get("metadata") or {}
        ref = format_ref(blogger_id, meta) or ""
        snippet = (c.get("document") or "")[:700]
        lines.append(f"[{i}] ref:{ref}\n{snippet}")

    system = (
        "Ты — модуль отбора контента. Оцени фрагменты для ответа консультанта.\n" + RERANK_SCHEMA
    )
    um = _sanitize_api_text(user_message, 4000)
    wuk = _safe_json_for_prompt(what_user_already_knows, 3000)
    an = _safe_json_for_prompt(analysis, 1200)
    user = (
        f"Сообщение пользователя: {um}\n"
        f"Уже знает (не дублировать): {wuk}\n"
        f"Краткий анализ: {an}\n\n"
        "Фрагменты:\n" + "\n\n".join(lines)
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )
    _add_usage(usage_acc, resp)
    stage_timings_ms["rerank"] = (time.perf_counter() - t0) * 1000
    raw = resp.choices[0].message.content or '{"rankings":[]}'
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return candidates[:max_keep]

    keep_indices: set[int] = set()
    rankings = data.get("rankings") or []
    for r in rankings:
        try:
            idx = int(r.get("index", -1))
            rel = float(r.get("relevance", 0))
            cit = float(r.get("citation_value", 0))
            keep = r.get("keep", True)
            if keep and min(rel, cit) >= 7 and 0 <= idx < len(candidates):
                keep_indices.add(idx)
        except (TypeError, ValueError):
            continue

    if not keep_indices:
        keep_indices = set(range(min(max_keep, len(candidates))))

    ordered = sorted(keep_indices)[:max_keep]
    return [candidates[i] for i in ordered if i < len(candidates)]


def build_content_package(
    ranked: list[dict[str, Any]],
    blogger_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Human-readable block for generation + sources list for API."""
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    for c in ranked:
        meta = c.get("metadata") or {}
        doc = (c.get("document") or "").strip()
        ref = format_ref(blogger_id, meta)
        title = meta.get("summary") or meta.get("content_type") or "фрагмент"
        cite = (
            f"В материале ({title}) я разбираю похожую линию; опора для цитирования: {ref}"
            if ref
            else f"Фрагмент ({title})"
        )
        parts.append(f"{cite}\n---\n{doc[:2000]}")
        sources.append(
            {
                "chunk": doc[:200],
                "ref": ref,
                "similarity": round(1 - float(c.get("distance", 1)), 3),
            }
        )
    return "\n\n═══\n\n".join(parts), sources


async def run_generation_agent(
    client: AsyncOpenAI,
    *,
    tone_of_voice: str,
    few_shot_dialogues: str,
    disclaimer: str,
    user_message: str,
    response_plan: dict[str, Any],
    what_user_already_knows: list[str],
    do_not: str,
    content_block: str,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    user_turn_index: int = 1,
    anti_repeat_block: str = "",
) -> str:
    t0 = time.perf_counter()
    fs = f"\n\nПРИМЕРЫ ТОНА (few-shot):\n{few_shot_dialogues}\n" if few_shot_dialogues.strip() else ""
    arc = ""
    if user_turn_index > 1:
        arc = (
            f"\nЭто не первый ход диалога (сообщение пользователя №{user_turn_index}). "
            "Развивай нить: не начинай с нуля и не повторяй тот же каркас абзацев, что в прошлых ответах. "
            "Меняй заход: можно без «Скажем так» в начале, можно один плотный абзац и один вопрос. "
            "Не выдавай список универсальных техник (дыхание, пять чувств, дневник), если это не единственный смысл плана — "
            "и не повторяй их, если они уже звучали в истории.\n"
        )
    anti = _sanitize_api_text(anti_repeat_block, 4500) if anti_repeat_block else ""
    anti_user = f"\n\nАНТИ-ШАБЛОН:\n{anti}\n" if anti else ""

    system = (
        tone_of_voice
        + fs
        + arc
        + (
            "\n\nСейчас ты только генерируешь текст ответа пользователю. "
            "Анализ уже сделан — не перечисляй внутренние этапы и номера гипотез. "
            "Не повторяй банально то, что пользователь уже знает (см. список). "
            "Если в плане есть цитата/опора — вплети естественно, без дословного копирования длинных кусков."
        )
    )
    plan_s = _safe_json_for_prompt(response_plan, 8000)
    wuk_s = _safe_json_for_prompt(what_user_already_knows, 4000)
    do_s = _sanitize_api_text(do_not, 4000)
    cb = _sanitize_api_text(content_block, 12000)
    um = _sanitize_api_text(user_message, 6000)
    disc = _sanitize_api_text(disclaimer, 2000)
    user = (
        f"ПЛАН ОТВЕТА:\n{plan_s}\n\n"
        f"Пользователь УЖЕ ЗНАЕТ (не повторять как открытие):\n{wuk_s}\n\n"
        f"ЗАПРЕЩЕНО в этом ответе:\n{do_s}\n\n"
        f"КОНТЕКСТ ДЛЯ ОПОРЫ (не пересказывать дословно):\n{cb or '(нет подобранных фрагментов — опирайся на методологию и план)'}\n\n"
        f"Сообщение пользователя:\n{um}\n"
        f"{anti_user}"
        f"{disc}\n\n"
        "Ответь одним связным сообщением. Если в плане указано задать один уточняющий вопрос — один в конце. "
        "Если в плане сказано не давать задание — не давай."
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.35,
        max_tokens=1200,
    )
    _add_usage(usage_acc, resp)
    stage_timings_ms["generation"] = (time.perf_counter() - t0) * 1000
    return (resp.choices[0].message.content or "").strip()


JUDGE_SCHEMA = """
Оцени черновик по шкале 1-10 (целые) по критериям:
persona_fidelity, information_gain, anti_parroting, methodology_presence, citation_quality, actionability, captain_obvious,
structural_freshness (высокий балл = ответ НЕ копирует каркас и набор блоков недавних ответов ассистента; низкий = тот же опросник/техники/абзацы)

Для captain_obvious: высокий балл = мало банальностей и нет набора «универсального коучинга» (дыхание, пять чувств, дневник) без явной нужды.

Если в запросе секция «Недавние ответы ассистента» пустая или (первый ответ) — ставь structural_freshness = 10 и не используй его как причину провала.

Пороги провала: anti_parroting < 7 ИЛИ information_gain < 6 ИЛИ persona_fidelity < 6 ИЛИ methodology_presence < 5
ИЛИ citation_quality < 5 (если в плане предполагалась опора на контент и контент был)
ИЛИ actionability < 5 ИЛИ captain_obvious < 6
ИЛИ structural_freshness < 6 (только если были недавние ответы ассистента в контексте)

Верни ТОЛЬКО один JSON-объект с ключами:
scores (объект с числовыми полями по всем критериям выше),
passed (true или false),
blocking_issues (массив объектов: criterion, score, evidence, fix_instruction),
rewrite_instructions (строка).
"""


async def run_quality_judge(
    client: AsyncOpenAI,
    *,
    draft: str,
    user_message: str,
    what_user_already_knows: list[str],
    do_not: str,
    response_plan: dict[str, Any],
    had_content: bool,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    anti_repeat_block: str = "",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    system = (
        "Ты — модуль контроля качества ответов. Ты НЕ генерируешь ответ пользователю.\n" + JUDGE_SCHEMA
    )
    draft_s = _sanitize_api_text(draft, 8000)
    um_s = _sanitize_api_text(user_message, 4000)
    do_s = _sanitize_api_text(do_not, 2000)
    wuk = _safe_json_for_prompt(what_user_already_knows, 4000)
    plan_s = _safe_json_for_prompt(response_plan, 8000)
    ar = _sanitize_api_text(anti_repeat_block, 4500) if anti_repeat_block else ""
    recent_section = (
        f"Недавние ответы ассистента в сессии (сравни с черновиком — нет ли того же шаблона):\n{ar}\n\n"
        if ar
        else "Недавние ответы ассистента: (нет — первый ответ в ветке)\n\n"
    )
    user = (
        f"Черновик:\n{draft_s}\n\n"
        f"Сообщение пользователя:\n{um_s}\n\n"
        f"Уже знает:\n{wuk}\n\n"
        f"DO_NOT:\n{do_s}\n\n"
        f"План:\n{plan_s}\n\n"
        f"{recent_section}"
        f"В плане предполагалась опора на контент из базы: {had_content}\n"
    )
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=1000,
    )
    try:
        resp = await client.chat.completions.create(
            **kwargs,
            response_format={"type": "json_object"},
        )
    except APIStatusError as e:
        if getattr(e, "status_code", None) == 400:
            logger.warning("quality_judge_json_mode_retry", detail=str(e)[:400])
            resp = await client.chat.completions.create(**kwargs)
        else:
            raise
    _add_usage(usage_acc, resp)
    stage_timings_ms["judge"] = (time.perf_counter() - t0) * 1000
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"passed": True, "scores": {}, "blocking_issues": [], "rewrite_instructions": ""}


async def run_rewrite(
    client: AsyncOpenAI,
    *,
    tone_of_voice: str,
    draft: str,
    rewrite_instructions: str,
    user_message: str,
    model: str,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    tag: str = "rewrite",
) -> str:
    t0 = time.perf_counter()
    d = _sanitize_api_text(draft, 8000)
    ri = _sanitize_api_text(rewrite_instructions, 4000)
    um = _sanitize_api_text(user_message, 6000)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": tone_of_voice + "\n\nПерепиши ответ по инструкции. Один связный текст."},
            {
                "role": "user",
                "content": f"Инструкция правки:\n{ri}\n\nЧерновик:\n{d}\n\nИсходный вопрос пользователя:\n{um}",
            },
        ],
        temperature=0.3,
        max_tokens=1200,
    )
    _add_usage(usage_acc, resp)
    stage_timings_ms[tag] = (time.perf_counter() - t0) * 1000
    return (resp.choices[0].message.content or d).strip()
