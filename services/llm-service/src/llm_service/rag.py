"""RAG engine: embed query → search ChromaDB → build context → call LLM."""
import structlog
import chromadb
from openai import AsyncOpenAI

from llm_service.config import settings, load_blogger_config

logger = structlog.get_logger()


def get_chroma_collection(blogger_id: str):
    cfg = load_blogger_config(blogger_id)
    client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    collection_name = cfg.get("chroma_collection", f"blogger_{blogger_id}")
    return client.get_or_create_collection(name=collection_name)


async def rag_answer(
    query: str,
    blogger_id: str,
    chat_history: list[dict] | None = None,
    user_profile: dict | None = None,
) -> dict:
    cfg = load_blogger_config(blogger_id)
    rag_cfg = cfg.get("rag", {})
    top_k = rag_cfg.get("top_k", 5)
    max_context_chars = rag_cfg.get("max_context_chars", 6000)
    system_prompt = cfg.get("tone_of_voice_prompt", "")
    disclaimer = cfg.get("legal_disclaimer", "")

    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    emb_resp = await openai_client.embeddings.create(
        model=settings.EMBED_MODEL, input=query,
    )
    query_embedding = emb_resp.data[0].embedding

    collection = get_chroma_collection(blogger_id)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0] if results["documents"] else []
    metas = results["metadatas"][0] if results["metadatas"] else []
    distances = results["distances"][0] if results["distances"] else []

    context_pieces = []
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, distances), 1):
        sim = 1 - dist
        snippet = (doc or "").strip()
        if len(snippet) < 10:
            continue
        context_pieces.append(f"[{i}] {snippet}")

    context_text = "\n\n".join(context_pieces)[:max_context_chars]

    profile_block = ""
    if user_profile:
        import json as _json
        profile_block = f"\n\n═══ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ═══\n{_json.dumps(user_profile, ensure_ascii=False, indent=2)}\n═══════════════════════════\nУчитывай профиль при ответе. Обращайся по имени если оно указано."

    messages = [{"role": "system", "content": system_prompt + profile_block}]

    if chat_history:
        for msg in chat_history[-10:]:
            messages.append(msg)

    user_prompt = f"Вопрос: {query}"
    if context_text:
        user_prompt += f"\n\nКонтекст из базы знаний:\n{context_text}"
    user_prompt += f"\n\n{disclaimer}\n\nОтветь на вопрос, опираясь на контекст."

    messages.append({"role": "user", "content": user_prompt})

    chat_resp = await openai_client.chat.completions.create(
        model=settings.CHAT_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=1000,
    )

    answer = chat_resp.choices[0].message.content.strip()
    usage = {
        "prompt_tokens": chat_resp.usage.prompt_tokens,
        "completion_tokens": chat_resp.usage.completion_tokens,
    }

    logger.info("rag_answer", blogger=blogger_id, query_len=len(query),
                context_chunks=len(context_pieces), answer_len=len(answer))

    return {
        "answer": answer,
        "sources": [
            {"chunk": doc[:200], "similarity": round(1 - dist, 3)}
            for doc, dist in zip(docs[:3], distances[:3])
        ],
        "usage": usage,
    }


async def analyze_onboarding(
    responses: list[dict],
    blogger_id: str,
    user_name: str | None = None,
) -> dict:
    """Analyze onboarding responses → problem zones + hypotheses + next step."""
    cfg = load_blogger_config(blogger_id)
    system_prompt = cfg.get("tone_of_voice_prompt", "")
    analysis_prompt = cfg.get("analysis_prompt", "")

    responses_text = "\n".join(
        f"- {r.get('step_id', '?')}: {r.get('answer_value', '?')}"
        for r in responses
    )

    name = user_name or "пользователь"

    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    collection = get_chroma_collection(blogger_id)
    symptoms = [r.get("answer_value", "") for r in responses if r.get("step_id") == "symptoms"]
    symptom_query = " ".join(symptoms) if symptoms else "психосоматика здоровье"

    emb = await openai_client.embeddings.create(model=settings.EMBED_MODEL, input=symptom_query)
    results = collection.query(query_embeddings=[emb.data[0].embedding], n_results=5, include=["documents"])
    context = "\n".join(results["documents"][0][:3]) if results["documents"] else ""

    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + analysis_prompt},
        {"role": "user", "content": (
            f"Имя пользователя: {name}\n\n"
            f"Ответы из онбординга:\n{responses_text}\n\n"
            f"Релевантный контекст из базы знаний:\n{context[:3000]}\n\n"
            f"Проанализируй и дай результат по структуре."
        )},
    ]

    resp = await openai_client.chat.completions.create(
        model=settings.CHAT_MODEL, messages=messages,
        temperature=0.4, max_tokens=2000,
    )

    answer = resp.choices[0].message.content.strip()
    return {
        "analysis": answer,
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        },
    }


async def update_user_profile(
    messages: list[dict],
    current_profile: dict | None,
    blogger_id: str,
    user_name: str | None = None,
) -> dict:
    """Session-updater agent (per §14.3): analyze dialogue → update profile fields."""
    import json as _json

    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    current = _json.dumps(current_profile or {}, ensure_ascii=False, indent=2)
    dialogue = "\n".join(f"[{m['role']}]: {m['content'][:500]}" for m in messages[-20:])

    prompt = f"""Ты — агент-апдейтер профиля пользователя. Проанализируй диалог и реши, 
нужно ли обновить поля профиля. Если в сессии не было новой значимой информации — верни текущий профиль без изменений.

ПОЛЯ ПРОФИЛЯ (§14.4):
- name: как пользователь просит себя называть (только если явно назвал)
- communication_style: формально/неформально, короткие/развёрнутые ответы (из паттерна переписки)
- goals: чего хочет достичь (только если говорил явно)
- topics_of_interest: темы из диалогов (ЖКТ, сон, тревога и т.д.)
- reactions: что заходило хорошо, что вызвало негатив (только явные сигналы)
- last_session_summary: 3-5 предложений о чём была эта сессия
- previous_session_summary: перенеси сюда старый last_session_summary

ПРАВИЛА:
- Не домысливай возраст, профессию, эмоциональное состояние
- Сжимай устаревшие данные, не дописывай поверх
- Итоговый JSON должен быть ≤ 4000 символов
- Верни ТОЛЬКО валидный JSON, без markdown

ТЕКУЩИЙ ПРОФИЛЬ:
{current}

ДИАЛОГ:
{dialogue}

Верни обновлённый JSON профиля:"""

    resp = await openai_client.chat.completions.create(
        model=settings.CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=1000,
        response_format={"type": "json_object"},
    )

    try:
        updated = _json.loads(resp.choices[0].message.content)
    except _json.JSONDecodeError:
        updated = current_profile or {}

    summary = updated.get("last_session_summary", "")

    return {
        "profile": updated,
        "summary": summary,
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        },
    }
