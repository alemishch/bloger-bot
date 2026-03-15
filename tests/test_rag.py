# tests/test_rag.py
# Simple RAG answer generator (Russian). Edit QUERY to test.
import os
from dotenv import load_dotenv

load_dotenv()

# ===================== Edit this query =====================
QUERY = "как справиться с усталостью и восстановить энергию"
# ===========================================================
TOP_K = 5
EMBED_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-4o-mini"
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

def main():
    try:
        import chromadb
    except Exception:
        raise SystemExit("Install chromadb-client: pip install chromadb-client")

    try:
        from openai import OpenAI
    except Exception:
        raise SystemExit("Install openai: pip install openai")

    if not OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY not set in environment (.env)")

    # Connect to ChromaDB
    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    cols = client.list_collections()
    if not cols:
        raise SystemExit("No ChromaDB collections found.")
    collection = client.get_collection(cols[0].name)
    print(f"Using collection: {cols[0].name} (chunks={collection.count()})\n")

    # Embed query
    oai = OpenAI(api_key=OPENAI_API_KEY)
    emb_resp = oai.embeddings.create(model=EMBED_MODEL, input=QUERY)
    q_emb = emb_resp.data[0].embedding

    # Query Chroma for top chunks
    res = collection.query(
        query_embeddings=[q_emb],
        n_results=TOP_K,
        include=["documents", "metadatas", "distances"],
    )

    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]

    # Build context for prompt
    context_pieces = []
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), start=1):
        sim = 1 - dist
        snippet = (doc or "").strip().replace("\n", " ")
        context_pieces.append(f"[{i}] (sim={sim:.3f}) {snippet}\nMETADATA: {meta}")

    context_text = "\n\n".join(context_pieces)

    system_prompt = (
        "You are an expert assistant in Russian. Use ONLY the provided context sections to answer the user's question. "
        "If the context does not contain relevant information, say you cannot find an answer. Cite section numbers in the answer, "
        "and keep the answer concise (3-6 sentences)."
    )

    user_prompt = (
        f"Question: {QUERY}\n\n"
        f"Context (top {TOP_K} retrieved chunks):\n{context_text}\n\n"
        "Provide a helpful, evidence-based answer in Russian and reference the section numbers you used."
    )

    # Ask the model
    chat = oai.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=800,
    )

    assistant_text = chat.choices[0].message.content.strip()
    print("===== ANSWER =====\n")
    print(assistant_text)
    print("\n===== RETRIEVED CHUNKS =====\n")
    for i, piece in enumerate(context_pieces, start=1):
        print(f"--- [{i}] ---\n{piece}\n")

if __name__ == "__main__":
    main()