"""Orchestrated multi-agent conversation pipeline (v2)."""

from __future__ import annotations

import os
from typing import Any

import structlog
from openai import AsyncOpenAI

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

    stage_model = os.getenv("OPENAI_STAGE_MODEL", settings.OPENAI_STAGE_MODEL)
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

    packed = pack_context(
        chat_history,
        user_profile,
        dialogue_phase=dialogue_phase or "free_chat",
        working_turns=int(cfg.get("pipeline_working_turns", 6)),
    )

    user_turn = count_user_turns(chat_history)
    anti_repeat = build_anti_repetition_block(
        chat_history,
        max_assistant_messages=int(cfg.get("pipeline_anti_repeat_assistants", 4)),
    )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    usage_acc: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    timings: dict[str, float] = {}

    analysis = await run_analysis_agent(
        client,
        user_message=query,
        packed_working_memory=packed["working_memory_text"],
        packed_profile=packed["profile_text"],
        dialogue_phase=packed["dialogue_phase"],
        model=stage_model,
        usage_acc=usage_acc,
        stage_timings_ms=timings,
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

    if _llm_debug_stages():
        result["debug"] = {
            "analysis": analysis,
            "hypothesis": hypothesis,
            "judge": judge,
            "timings_ms": timings,
            "queries": unique_queries,
        }

    return result
