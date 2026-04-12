"""Orchestrated multi-agent conversation pipeline (v2)."""

from __future__ import annotations

import os
from typing import Any

import structlog

from llm_service.agents import (
    build_content_package,
    run_analysis_agent,
    run_generation_agent,
    run_hypothesis_agent,
    run_quality_judge,
    run_rerank_chunks,
    run_rewrite,
)
from llm_service.config import load_blogger_config, settings
from llm_service.context_pack import (
    build_anti_repetition_block,
    count_user_turns,
    pack_context,
)
from llm_service.openai_compat import pipeline_stage_model
from llm_service.openai_http import async_openai_client
from llm_service.phase_gate import (
    days_since_phase_start,
    rejection_hint_for_hypothesis,
    resolve_phase_transition,
    utc_today,
)
from llm_service.phases import (
    append_phase_log,
    build_phase_context_block,
    default_phase_id,
    get_phase_by_id,
    load_phase_config,
    next_phase_id,
    phase_catalog_for_prompt,
)
from llm_service.retrieval import multi_query_retrieve

logger = structlog.get_logger()


def _llm_debug_stages() -> bool:
    v = os.getenv("LLM_DEBUG_STAGES", "").strip().lower()
    return v in ("1", "true", "yes")


async def conversation_pipeline_answer(
    query: str,
    blogger_id: str,
    chat_history: list[dict] | None = None,
    user_profile: dict | None = None,
    dialogue_phase: str | None = None,
) -> dict[str, Any]:
    cfg = load_blogger_config(blogger_id)
    rag_cfg = cfg.get("rag", {})
    retrieve_n = min(int(rag_cfg.get("pipeline_retrieve_n", 14)), 30)

    stage_model = pipeline_stage_model()
    chat_model = settings.CHAT_MODEL

    methodology = (cfg.get("methodology_framework") or "").strip()
    if not methodology:
        methodology = (
            "Два типа причин повторяющихся сценариев: АВТОМАТИЗМЫ (меняются осознанностью) и "
            "БЕССОЗНАТЕЛЬНЫЕ ПРОГРАММЫ (осознанностью не снять). Знание «почему» без смены программы — самообман. "
            "Биология первична. Симптом как сигнал адаптации. Роли: врач, остеопат, клинический психолог, "
            "гипнотерапевт, биодекодирование, энергопрактик, ГНМ — только как линзы, не смешивать всё сразу."
        )

    few_shots = (cfg.get("few_shot_dialogues") or "").strip()
    tone = cfg.get("tone_of_voice_prompt", "")
    disclaimer = cfg.get("legal_disclaimer", "")

    phases, ordered_ids = load_phase_config(blogger_id)
    profile: dict[str, Any] = user_profile if isinstance(user_profile, dict) else {}

    raw_phase = (profile.get("dialogue_phase") or "").strip()
    phase_before = raw_phase if raw_phase in ordered_ids else default_phase_id(ordered_ids)

    active_before = get_phase_by_id(phases, phase_before)
    if not active_before and phases:
        active_before = phases[0]
        phase_before = active_before.id

    dsp = days_since_phase_start(profile.get("phase_started_at"))
    days_in_phase = dsp if dsp is not None else 0

    next_title: str | None = None
    nxt_id = next_phase_id(ordered_ids, phase_before)
    if nxt_id:
        np = get_phase_by_id(phases, nxt_id)
        next_title = (np.title if np else None) or nxt_id

    pcb = ""
    if active_before:
        pcb = build_phase_context_block(
            phases=phases,
            ordered_ids=ordered_ids,
            active=active_before,
            days_in_phase=days_in_phase,
            next_title=next_title,
        )

    phase_catalog = phase_catalog_for_prompt(phases)

    packed = pack_context(
        chat_history,
        user_profile,
        dialogue_phase=dialogue_phase or phase_before,
        working_turns=int(cfg.get("pipeline_working_turns", 6)),
        dialogue_phase_override=phase_before,
    )

    user_turn = count_user_turns(chat_history)
    anti_repeat = build_anti_repetition_block(
        chat_history,
        max_assistant_messages=int(cfg.get("pipeline_anti_repeat_assistants", 4)),
    )

    client = async_openai_client()
    usage_acc: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    timings: dict[str, float] = {}

    first_phase_analysis_hint = ""
    if ordered_ids and phase_before == ordered_ids[0]:
        first_phase_analysis_hint = (
            "Сейчас ПЕРВАЯ фаза (сбор как ~3 визита психолога): phase_readiness.enough_for_hypothesis держи false, "
            "пока нет богатой картины (история, контекст, что пробовали, повторы во времени). "
            "phase_transition.sufficient_understanding=true только если по объёму данных это правда сопоставимо с тремя полноценными сессиями; иначе false и не запрашивай переход вперёд."
        )

    analysis = await run_analysis_agent(
        client,
        user_message=query,
        packed_working_memory=packed["working_memory_text"],
        packed_profile=packed["profile_text"],
        dialogue_phase=packed["dialogue_phase"],
        phase_catalog=phase_catalog,
        phase_context_block=pcb,
        model=stage_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
        first_phase_analysis_hint=first_phase_analysis_hint,
    )

    transition = analysis.get("phase_transition")
    if not isinstance(transition, dict):
        transition = {}

    _eff_id, _eff_started, gate_delta, gdebug = resolve_phase_transition(
        phases=phases,
        ordered_ids=ordered_ids,
        profile_phase_id=profile.get("dialogue_phase"),
        profile_phase_started_at=profile.get("phase_started_at"),
        transition=transition,
        ensure_start_date=utc_today().isoformat(),
    )

    notes = gdebug.get("gate_notes") or []
    gate_hint = rejection_hint_for_hypothesis([str(x) for x in notes])

    active_eff = get_phase_by_id(phases, _eff_id) or active_before
    phase_focus_block = ""
    if active_eff:
        phase_focus_block = f"Фаза: {active_eff.id} — {active_eff.title}\n{active_eff.prompt_injection.strip()}"

    in_first_phase = bool(ordered_ids and _eff_id == ordered_ids[0])
    if in_first_phase:
        phase_focus_block = (
            "РЕЖИМ: первая фаза — как ~3 визита психолога только на сбор картины, без «терапии».\n"
            "response_plan: strategy и next_action — уточняющие вопросы и прояснение; key_insight — короткое отражение, не вердикт. "
            "hypotheses: 0–1 очень мягкая наводка или пусто; не строй развёрнутую клинику в JSON. "
            "DO_NOT должен явно запрещать планы лечения, назначения, длинные выводы «вам нужно…», списки техник.\n\n"
            + phase_focus_block
        )

    profile_delta: dict[str, Any] = {k: v for k, v in gate_delta.items() if v is not None}
    if profile_delta.get("dialogue_phase") and profile_delta["dialogue_phase"] != phase_before:
        profile_delta["phase_log"] = append_phase_log(
            profile.get("phase_log"),
            from_phase=phase_before,
            to_phase=profile_delta["dialogue_phase"],
            reason=str(transition.get("reason") or "")[:500],
        )

    hypothesis = await run_hypothesis_agent(
        client,
        analysis=analysis,
        packed_profile=packed["profile_text"],
        methodology_framework=methodology,
        model=stage_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
        user_turn_index=user_turn,
        anti_repeat_block=anti_repeat,
        phase_gate_hint=gate_hint,
        phase_focus_block=phase_focus_block,
        long_memory_block=packed.get("long_memory_text") or "",
    )

    response_plan = hypothesis.get("response_plan") or {}
    if not isinstance(response_plan, dict):
        response_plan = {}
    do_not = str(response_plan.get("DO_NOT") or "не пересказывать пользователя дословно")
    what_knows = analysis.get("what_user_already_knows") or []
    if not isinstance(what_knows, list):
        what_knows = []

    queries: list[str] = [query[:800]]
    for h in (hypothesis.get("hypotheses") or [])[:3]:
        if isinstance(h, dict):
            ct = h.get("content_to_retrieve")
            if ct:
                queries.append(str(ct)[:500])
    for item in (response_plan.get("content_needed") or [])[:2]:
        queries.append(str(item)[:400])

    seen_q: set[str] = set()
    unique_queries: list[str] = []
    for q in queries:
        qn = q.strip()
        if qn and qn not in seen_q:
            seen_q.add(qn)
            unique_queries.append(qn)
    unique_queries = unique_queries[:4]

    embeddings: list[list[float]] = []
    for qtext in unique_queries:
        emb_resp = await client.embeddings.create(model=settings.EMBED_MODEL, input=qtext)
        embeddings.append(emb_resp.data[0].embedding)

    candidates = await multi_query_retrieve(
        blogger_id,
        embeddings,
        n_results=retrieve_n,
    )

    ranked = await run_rerank_chunks(
        client,
        user_message=query,
        analysis=analysis,
        what_user_already_knows=what_knows,
        candidates=candidates,
        blogger_id=blogger_id,
        model=stage_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
        max_keep=int(rag_cfg.get("pipeline_max_chunks", 5)),
    )

    content_block, sources = build_content_package(ranked, blogger_id)
    had_content = bool(ranked)

    exploration_note = ""
    if in_first_phase:
        exploration_note = (
            "Сейчас первая фаза (сбор как у психолога): ответ в основном из вопросов и 1–2 предложений отражения. "
            "Не разворачивай «лечение», программы изменений и длинные рекомендации — даже если план тянет в эту сторону, смягчи к уточнению."
        )

    draft = await run_generation_agent(
        client,
        tone_of_voice=tone,
        few_shot_dialogues=few_shots,
        disclaimer=disclaimer,
        user_message=query,
        response_plan=response_plan,
        what_user_already_knows=what_knows,
        do_not=do_not,
        content_block=content_block,
        model=chat_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
        user_turn_index=user_turn,
        anti_repeat_block=anti_repeat,
        exploration_mode_note=exploration_note,
    )

    quality_warning = False
    judge = await run_quality_judge(
        client,
        draft=draft,
        user_message=query,
        what_user_already_knows=what_knows,
        do_not=do_not,
        response_plan=response_plan,
        had_content=had_content,
        model=stage_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
        anti_repeat_block=anti_repeat,
    )

    answer = draft
    rewrites = 0
    while not judge.get("passed", True) and rewrites < 2:
        instr = judge.get("rewrite_instructions") or ""
        if not instr:
            break
        answer = await run_rewrite(
            client,
            tone_of_voice=tone,
            draft=answer,
            rewrite_instructions=instr,
            user_message=query,
            model=chat_model,
            usage_acc=usage_acc,
            stage_timings_ms=timings,
            tag=f"rewrite_{rewrites + 1}",
        )
        rewrites += 1
        judge = await run_quality_judge(
            client,
            draft=answer,
            user_message=query,
            what_user_already_knows=what_knows,
            do_not=do_not,
            response_plan=response_plan,
            had_content=had_content,
            model=stage_model,
            usage_acc=usage_acc,
            stage_timings_ms=timings,
            anti_repeat_block=anti_repeat,
        )

    if not judge.get("passed", True):
        quality_warning = True

    logger.info(
        "conversation_pipeline_done",
        blogger=blogger_id,
        tokens=usage_acc,
        timings_ms=timings,
        quality_warning=quality_warning,
        rewrites=rewrites,
    )

    result: dict[str, Any] = {
        "answer": answer,
        "sources": sources,
        "usage": usage_acc,
        "quality_warning": quality_warning,
    }

    if profile_delta:
        result["profile_delta"] = profile_delta

    if _llm_debug_stages():
        result["debug"] = {
            "analysis": analysis,
            "hypothesis": hypothesis,
            "judge": judge,
            "timings_ms": timings,
            "queries": unique_queries,
            "phase_gate": gdebug,
        }

    return result
