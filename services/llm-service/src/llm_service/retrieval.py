"""Multi-query Chroma retrieval for the conversation pipeline.

Phase 2 (deferred): BM25 / hybrid search — see plan; Chroma has no built-in BM25.
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

import structlog

from llm_service.rag import get_chroma_collection

logger = structlog.get_logger()


def _dedupe_key(meta: dict | None, doc: str, idx: int) -> str:
    meta = meta or {}
    item = meta.get("item_id")
    chunk_i = meta.get("chunk_index")
    if item is not None and chunk_i is not None:
        return f"{item}:{chunk_i}"
    return f"hash:{hash((doc or '')[:200])}:{idx}"


async def multi_query_retrieve(
    blogger_id: str,
    query_embeddings: list[list[float]],
    n_results: int = 14,
) -> list[dict[str, Any]]:
    """
    Run parallel Chroma queries, merge and dedupe by item_id:chunk_index.
    Returns list of {document, metadata, distance, best_distance}.
    """
    if not query_embeddings:
        return []

    collection = get_chroma_collection(blogger_id)
    merged: dict[str, dict[str, Any]] = {}

    for qemb in query_embeddings:
        results = collection.query(
            query_embeddings=[qemb],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        dists = results["distances"][0] if results["distances"] else []

        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
            doc = (doc or "").strip()
            if len(doc) < 10:
                continue
            key = _dedupe_key(meta, doc, i)
            dist = float(dist) if dist is not None else 1.0
            prev = merged.get(key)
            if prev is None or dist < prev["distance"]:
                merged[key] = {
                    "document": doc,
                    "metadata": meta or {},
                    "distance": dist,
                }

    out = list(merged.values())
    out.sort(key=lambda x: x["distance"])
    logger.info("multi_query_retrieve", blogger=blogger_id, unique_chunks=len(out))
    return out


def format_ref(blogger_id: str, meta: dict) -> str | None:
    item_id = meta.get("item_id")
    if item_id is None:
        return None
    chunk_index = meta.get("chunk_index", "?")
    return f"ref:{blogger_id}:{item_id}:{chunk_index}"
