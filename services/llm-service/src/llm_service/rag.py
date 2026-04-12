"""RAG engine: embed query → search ChromaDB → build context → call LLM."""
import structlog
import chromadb
from openai import AsyncOpenAI

from llm_service.config import settings, load_blogger_config

logger = structlog.get_logger()

SOURCE_TEXT_PREVIEW_CHARS = 1000


def get_chroma_collection(blogger_id: str):
    """Open existing Chroma collection (do not create empty — that hides misconfig)."""
    cfg = load_blogger_config(blogger_id)
    client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    collection_name = cfg.get("chroma_collection", f"blogger_{blogger_id}")
    return client.get_collection(name=collection_name)


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
    collection_name = cfg.get("chroma_collection", f"blogger_{blogger_id}")
    chroma_count = collection.count()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0] if results["documents"] else []
    metas = results["metadatas"][0] if results["metadatas"] else []
    distances = results["distances"][0] if results["distances"] else []
    row_ids = results["ids"][0] if results.get("ids") else []

    context_pieces = []
    sources: list[dict] = []
    ctx_idx = 0
    pairs = zip(docs, metas, distances, row_ids or [None] * len(docs))
    for doc, meta, dist, chroma_id in pairs:
        snippet = (doc or "").strip()
        if len(snippet) < 10:
            continue

        ctx_idx += 1
        meta = meta or {}
        item_id = meta.get("item_id")
        chunk_index = meta.get("chunk_index")
        idx_display = chunk_index if chunk_index is not None else "?"
        ref = (
            f"ref:{blogger_id}:{item_id}:{idx_display}" if item_id is not None else ""
        )

        meta_parts: list[str] = []
        if meta.get("content_type"):
            meta_parts.append(str(meta.get("content_type")))
        if meta.get("tags"):
            meta_parts.append(f"tags:{str(meta.get('tags'))[:80]}")
        if meta.get("summary"):
            meta_parts.append(f"summary:{str(meta.get('summary'))[:160]}")
        if item_id is not None:
            meta_parts.append(ref)
        if meta.get("source_message_id"):
            meta_parts.append(f"source_msg:{meta.get('source_message_id')}")

        meta_label = " | ".join(meta_parts)
        context_pieces.append(f"[{ctx_idx}] {meta_label}\n{snippet}")

        sim = round(1 - dist, 3)
        preview = snippet[:SOURCE_TEXT_PREVIEW_CHARS]
        src: dict = {
            "id": chroma_id,
            "similarity": sim,
            "text": preview,
            "item_id": str(item_id) if item_id is not None else None,
            "chunk_index": int(chunk_index) if chunk_index is not None else None,
            "ref": ref,
            "chunk": preview[:200],
        }
        sources.append(src)

    context_text = "\n\n".join(context_pieces)[:max_context_chars]

    if not context_pieces:
        logger.warning(
            "rag_no_context",
            blogger=blogger_id,
            collection=collection_name,
            chroma_count=chroma_count,
            raw_docs=len(docs),
            query_len=len(query),
        )

    profile_block = ""
    if user_profile:
        import json as _json
        profile_block = f"\n\n═══ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ═══\n{_json.dumps(user_profile, ensure_ascii=False, indent=2)}\n═══════════════════════════\nУчитывай профиль при ответе. Обращайся по имени если оно указано."

    messages = [{"role": "system", "content": system_prompt + profile_block}]

    if chat_history:
        for msg in chat_history[-10:]:
            messages.append(msg)

    pending_question = None
    if chat_history:
        # Ищем последний уточняющий вопрос из предыдущего сообщения ассистента.
        for msg in reversed(chat_history):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                content = msg.get("content")
                marker = "Уточняющий вопрос:"
                if marker in content:
                    # Делаем мягкий парсинг — просто берём всё после маркера.
                    pending_question = content.split(marker, 1)[1].strip().splitlines()[0]
                    break

    if pending_question:
        user_prompt = (
            "СЦЕНАРИЙ: это ответ на уточняющий вопрос из предыдущего сообщения.\n"
            f"Уточняющий вопрос: {pending_question}\n"
            f"Ответ пользователя: {query}"
        )
    else:
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
        "sources": sources,
        "usage": usage,
        "retrieval": {
            "chroma_collection": collection_name,
            "chroma_document_count": chroma_count,
            "chunks_in_context": len(context_pieces),
        },
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
    # Retrieval по всему профилю анкеты — чтобы гипотезы не были “только про симптомы”.
    answer_query_parts: list[str] = []
    for r in responses:
        sid = r.get("step_id", "")
        aval = r.get("answer_value", "")
        if aval:
            answer_query_parts.append(f"{sid}: {aval}")
    answer_query = " ".join(answer_query_parts).strip()[:1200] or "психосоматика здоровье"

    emb = await openai_client.embeddings.create(model=settings.EMBED_MODEL, input=answer_query)
    results = collection.query(
        query_embeddings=[emb.data[0].embedding],
        n_results=8,
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0] if results["documents"] else []
    metas = results["metadatas"][0] if results["metadatas"] else []
    distances = results["distances"][0] if results["distances"] else []

    context_pieces: list[str] = []
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, distances), 1):
        snippet = (doc or "").strip()
        if len(snippet) < 30:
            continue

        meta = meta or {}
        meta_parts: list[str] = []
        if meta.get("content_type"):
            meta_parts.append(str(meta.get("content_type")))
        if meta.get("tags"):
            meta_parts.append(f"tags:{str(meta.get('tags'))[:70]}")
        if meta.get("summary"):
            meta_parts.append(f"summary:{str(meta.get('summary'))[:140]}")
        if meta.get("item_id"):
            meta_parts.append(f"ref:{blogger_id}:{meta.get('item_id')}:{meta.get('chunk_index', '?')}")
        if meta.get("source_message_id"):
            meta_parts.append(f"source_msg:{meta.get('source_message_id')}")
        meta_label = " | ".join(meta_parts)

        # Сохраняем индексы — модель должна ссылаться на них в ответе.
        context_pieces.append(f"[{i}] {meta_label}\n{snippet}")

    context = "\n\n".join(context_pieces)[:3000]

    # Доп. ограничения, чтобы LLM не уходил в “капитан очевидность”.
    # Эти требования должны выполняться даже если `analysis_prompt` в конфиге будет неполным.
    strict_rules = (
        "ФОРМАТ ОТВЕТА: верни ТОЛЬКО валидный JSON без markdown.\n"
        "JSON должен содержать ключи:\n"
        "- analysis_message: string (текст, который бот отправит пользователю)\n"
        "- profile_update: object (JSON для users.long_term_profile)\n"
        "- pending_question: string или null\n"
        "- confidence: число от 0 до 1\n"
        "\n"
        "profile_update заполни так (если данные отсутствуют — ставь пустые значения/{}):\n"
        "- communication_style (коротко: формально/неформально, предпочитает короткие/развёрнутые ответы)\n"
        "- goals (если пользователь явно говорил “хочу”, “мне нужно” — иначе пусто)\n"
        "- topics_of_interest (массив строк: например anxiety, gut, sleep, weight, pain)\n"
        "- reactions (объект: только явные позитив/негатив сигналы; иначе пусто)\n"
        "- last_session_summary (3–5 предложений: что понял бот, 1–3 зоны, почему это важно)\n"
        "ТРЕБОВАНИЯ К analysis_message:\n"
        "- Говори от первого лица ('я'), НЕ используй фразы вида “Юрий говорит/у Юрия/как говорит Юрий”.\n"
        "- НЕ пересказывай ответы пользователя дословно и не используй банальные формулы уровня “у тебя стресс”.\n"
        "- Выдели 1–3 проблемные зоны и к каждой 1–2 гипотезы (роль эксперта).\n"
        "- Опора на контент делается редко и только если усиливает гипотезы.\n"
        "  Вместо цитат давай референсы в формате поля `ref:` из CONTEXT: “Ссылка на материал: ref:...”.\n"
        "  Максимум 0–2 ссылки. Без длинных дословных цитат.\n"
        "- Либо: при confidence низкой (<=0.4) в конце добавь одну строку:\n"
        "  “Уточняющий вопрос: ...” (и НЕ предлагай задания/упражнения).\n"
        "- Либо: при confidence высокой (>0.4) в конце добавь одну строку:\n"
        "  “Ближайший шаг: ...” (обычно самоисследование; если это материал — максимум 1 ссылка, без цитат).\n"
        "- В конце не добавляй второе уточнение.\n"
    )

    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + analysis_prompt + "\n\n" + strict_rules},
        {"role": "user", "content": (
            f"Имя пользователя: {name}\n\n"
            f"Ответы из онбординга:\n{responses_text}\n\n"
            f"CONTEXT из базы знаний (используй индексы [n] как источники):\n{context}\n\n"
            f"Проанализируй и дай результат по структуре. Confidence оцени как честную уверенность по данным анкеты."
        )},
    ]

    resp = await openai_client.chat.completions.create(
        model=settings.CHAT_MODEL,
        messages=messages,
        temperature=0.35,
        max_tokens=2000,
        response_format={"type": "json_object"},
    )

    import json as _json

    try:
        payload = _json.loads(resp.choices[0].message.content)
    except _json.JSONDecodeError:
        payload = {
            "analysis_message": resp.choices[0].message.content.strip(),
            "profile_update": {},
            "pending_question": None,
            "confidence": 0.2,
        }

    analysis_message = payload.get("analysis_message") or payload.get("analysis") or ""
    pending_question = payload.get("pending_question")

    return {
        # Новая структура (используется telegram-bot).
        "analysis_message": analysis_message,
        "pending_question": pending_question,
        "confidence": payload.get("confidence", 0.0),
        "profile_update": payload.get("profile_update", {}),
        # Бэкап поле для совместимости со старым кодом.
        "analysis": analysis_message,
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
- pattern_summary: 2-4 предложения: повторяющиеся темы, автоматизмы, устойчивые реакции (только если проявились в диалоге; иначе оставь как в текущем профиле или пусто)
- previous_hypotheses: массив из 0-3 объектов {{"id": "H1", "zone": "...", "hypothesis": "кратко"}} — последние значимые гипотезы бота/пользователя в сессии; пустой массив если нечего фиксировать
- dialogue_phase: одно из: free_chat, exploration, deepening — только если по диалогу однозначно; иначе не меняй существующее значение

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
