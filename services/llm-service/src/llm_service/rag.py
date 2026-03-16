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

    messages = [{"role": "system", "content": system_prompt}]

    if chat_history:
        for msg in chat_history[-6:]:
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
