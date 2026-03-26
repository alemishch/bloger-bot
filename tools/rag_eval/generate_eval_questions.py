#!/usr/bin/env python3
"""Generate numbered eval questions from random Chroma chunks via LLM.

Run from repo root with deps: pip install chromadb openai pyyaml python-dotenv
  python tools/rag_eval/generate_eval_questions.py --count 50 --blogger-id yuri -o eval_questions.txt

Chroma from the host (Docker maps 8000:8000):
  python tools/rag_eval/generate_eval_questions.py -o eval_questions.txt --chroma-host localhost:8000

You can also pass --chroma-host localhost --chroma-port 8000. Do not combine host:8000 with --chroma-port
(old bug); the script now splits host:port from one flag.

If .env has CHROMA_HOST=chromadb (Docker-only name), this script uses localhost when host is still chromadb.

Env: OPENAI_API_KEY; optional CHROMA_HOST, CHROMA_PORT, CHAT_MODEL (default gpt-4o-mini).
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


def _split_chroma_host_port(raw: str, default_port: int) -> tuple[str, int]:
    """Return (host, port). Accepts host, host:port, http(s)://host:port, [::1]:port."""
    s = (raw or "").strip()
    if not s:
        return "localhost", default_port

    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        u = urlparse(s)
        h = u.hostname or "localhost"
        p = u.port if u.port is not None else default_port
        return h, p

    if s.startswith("[") and "]:" in s:
        end = s.index("]:")
        host_part, port_part = s[: end + 1], s[end + 2 :]
        if port_part.isdigit():
            return host_part, int(port_part)

    if ":" in s:
        base, _, maybe_port = s.rpartition(":")
        if base and maybe_port.isdigit():
            return base, int(maybe_port)

    return s, default_port


def _load_collection_name(blogger_id: str) -> str:
    import yaml

    cfg_path = ROOT / "config" / "bloggers" / f"{blogger_id}.yaml"
    if not cfg_path.is_file():
        raise SystemExit(f"Blogger config not found: {cfg_path}")
    raw = cfg_path.read_text(encoding="utf-8")
    expanded = os.path.expandvars(raw)
    data = yaml.safe_load(expanded) or {}
    return data.get("chroma_collection") or f"blogger_{blogger_id}"


def _strip_question_line(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^[\"']|[\"']$", "", text).strip()
    text = re.sub(r"^(вопрос|question)\s*[:：]\s*", "", text, flags=re.I)
    return text.strip()


def generate_question(openai_client, model: str, chunk_text: str) -> str:
    sys_msg = (
        "Ты помогаешь оценивать RAG-бота. По фрагменту из базы знаний придумай ровно один "
        "естественный вопрос, как его мог бы задать пользователь в Telegram. "
        "Вопрос должен быть ответим по смыслу из этого фрагмента. Язык вопроса — как у фрагмента. "
        "Ответь только текстом вопроса, без нумерации и пояснений."
    )
    user_msg = f"Фрагмент:\n\n{chunk_text[:12000]}"
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.5,
        max_tokens=200,
    )
    return _strip_question_line(resp.choices[0].message.content or "")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Sample Chroma chunks and generate eval questions.")
    parser.add_argument("--blogger-id", default="yuri", help="Blogger id (config/bloggers/{id}.yaml)")
    parser.add_argument("--count", type=int, default=50, help="Number of questions (default 50)")
    parser.add_argument("--output", "-o", required=True, help="Output .txt path")
    parser.add_argument(
        "--chroma-host",
        default=os.getenv("CHROMA_HOST", "localhost"),
        help="Chroma server host, or host:port, or http://host:port",
    )
    parser.add_argument(
        "--chroma-port",
        type=int,
        default=None,
        help="Chroma port if not included in --chroma-host (default: CHROMA_PORT or 8000)",
    )
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for sampling")
    parser.add_argument(
        "--max-chunk-chars",
        type=int,
        default=0,
        help="If >0, truncate written chunk text in the file to this length",
    )
    parser.add_argument(
        "--chat-model",
        default=os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        help="OpenAI chat model for question generation",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (e.g. in .env)")

    try:
        import chromadb
        from openai import OpenAI
    except ImportError as e:
        raise SystemExit(f"Missing dependency: {e}. pip install chromadb openai") from e

    env_port = int(os.getenv("CHROMA_PORT", "8000"))
    default_port = args.chroma_port if args.chroma_port is not None else env_port
    chroma_host, chroma_port = _split_chroma_host_port(args.chroma_host, default_port)

    if chroma_host == "chromadb":
        print(
            "Note: CHROMA_HOST=chromadb only resolves inside Docker; using localhost for this host run.",
            file=sys.stderr,
        )
        chroma_host = "localhost"

    collection_name = _load_collection_name(args.blogger_id)
    client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
    try:
        collection = client.get_collection(name=collection_name)
    except Exception as e:
        raise SystemExit(f"Chroma collection {collection_name!r}: {e}") from e

    n = collection.count()
    if n == 0:
        raise SystemExit(
            f"Collection {collection_name!r} is empty at {chroma_host}:{chroma_port}. "
            "Postgres can show many ready rows while Chroma has no vectors — re-run vectorization "
            "or restore the chroma_data Docker volume."
        )

    got = collection.get(include=["documents", "metadatas"])
    all_ids = got.get("ids") or []
    if not all_ids:
        raise SystemExit("No chunk ids returned from Chroma.")

    k = min(args.count, len(all_ids))
    sampled_ids = random.sample(all_ids, k=k)
    sampled = collection.get(ids=sampled_ids, include=["documents", "metadatas"])
    out_ids = sampled.get("ids") or []
    docs = sampled.get("documents") or []
    metas = sampled.get("metadatas") or []
    by_id: dict[str, tuple[str | None, dict]] = {}
    for cid, doc, meta in zip(out_ids, docs, metas):
        by_id[cid] = (doc, meta or {})

    oai = OpenAI(api_key=api_key)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for i, cid in enumerate(sampled_ids, start=1):
        doc, meta = by_id.get(cid, ("", {}))
        chunk_body = (doc or "").strip()
        question = generate_question(oai, args.chat_model, chunk_body)
        if not question:
            question = "[не удалось сгенерировать вопрос]"

        lines.append(f"=== {i} ===")
        lines.append(f"Question: {question}")
        lines.append("---")
        lines.append(f"id: {cid}")
        if meta:
            item_id = meta.get("item_id")
            cidx = meta.get("chunk_index")
            if item_id is not None:
                lines.append(f"item_id: {item_id}")
            if cidx is not None:
                lines.append(f"chunk_index: {cidx}")
            tags = meta.get("tags")
            if tags:
                lines.append(f"tags: {tags}")
            summary = meta.get("summary")
            if summary:
                lines.append(f"summary: {str(summary)[:300]}")
        body_out = chunk_body
        if args.max_chunk_chars and args.max_chunk_chars > 0:
            body_out = body_out[: args.max_chunk_chars]
        lines.append(body_out)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {k} questions to {out_path}")


if __name__ == "__main__":
    main()
