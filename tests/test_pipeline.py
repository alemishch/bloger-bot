"""
Pipeline integrity checker + minimal RAG test.

Validates each stage of the content pipeline against live PostgreSQL + ChromaDB.
RAG test requires OPENAI_API_KEY (costs ~$0.001 per run).

Run:
    python tests/check_pipeline.py              # all checks
    python tests/check_pipeline.py --no-rag     # skip RAG (no OpenAI needed)
    python tests/check_pipeline.py --stage rag  # only RAG
"""
import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# ── allow running from project root ─────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ingestion-service", "src"))

from dotenv import load_dotenv
load_dotenv()

import sqlalchemy as sa
from sqlalchemy.orm import Session

from common.models.enums import JobStatus, ContentType
from common.models.content import ContentItem, ContentSource
from common.config import DatabaseSettings, ChromaSettings

CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
VALID_CATEGORIES = {"education", "educational", "motivational", "case_study", "product_review", "personal_story"}

# Russian test queries for RAG
RAG_TEST_QUERIES = [
    "как справиться с усталостью и восстановить энергию",
    "проблемы с пищеварением и желудком",
    "психосоматика и здоровье",
    "как улучшить сон и качество отдыха",
    "снижение веса и правильное питание",
]

# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    issues: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def ok(self, msg: str):
        self.passed += 1
        self.details.append(f"  ✅ {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.issues.append(f"  ❌ {msg}")

    def warn(self, msg: str):
        self.warnings += 1
        self.issues.append(f"  ⚠️  {msg}")

    def print_summary(self):
        status = "PASS" if self.failed == 0 else "FAIL"
        color = "\033[32m" if self.failed == 0 else "\033[31m"
        reset = "\033[0m"
        print(f"\n{color}[{status}]{reset} {self.name}  "
              f"(✅ {self.passed}  ❌ {self.failed}  ⚠️  {self.warnings})")
        for line in self.details:
            print(line)
        for line in self.issues:
            print(line)


# ── DB helpers ────────────────────────────────────────────────────────────────

def make_engine():
    db = DatabaseSettings()
    return sa.create_engine(db.sync_url, echo=False)


def fetch_items_by_status(session: Session, status: JobStatus, limit: int = 200) -> list[ContentItem]:
    return session.execute(
        sa.select(ContentItem).where(ContentItem.status == status).limit(limit)
    ).scalars().all()


def fetch_all_items(session: Session, limit: int = 500) -> list[ContentItem]:
    return session.execute(
        sa.select(ContentItem).limit(limit)
    ).scalars().all()


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        sa.select(ContentItem.status, sa.func.count(ContentItem.id).label("cnt"))
        .group_by(ContentItem.status)
    ).fetchall()
    return {r.status.value: r.cnt for r in rows}


# ── Stage checks ──────────────────────────────────────────────────────────────

def check_overview(session: Session) -> CheckResult:
    r = CheckResult("Pipeline Overview")
    counts = count_by_status(session)
    total = sum(counts.values())

    if total == 0:
        r.fail("No content items found in DB at all")
        return r

    r.ok(f"Total items in DB: {total}")
    for status, cnt in sorted(counts.items()):
        if cnt > 0:
            r.details.append(f"    {status:<30} {cnt:>5}")

    # Check for stuck items
    problem_statuses = ["downloading", "transcribing", "labeling", "chunking"]
    for ps in problem_statuses:
        cnt = counts.get(ps, 0)
        if cnt > 5:
            r.warn(f"{cnt} items stuck in '{ps}' — may indicate worker crash")

    # Check for failures
    for fs in ["download_failed", "transcription_failed", "label_failed", "failed"]:
        cnt = counts.get(fs, 0)
        if cnt > 0:
            r.fail(f"{cnt} items in '{fs}' — needs investigation")

    downstream = sum(counts.get(s, 0) for s in ["transcribed", "labeled", "ready"])
    if downstream > 0:
        r.ok(f"Items reached transcription or beyond: {downstream}")
    else:
        r.warn("No items have reached transcribed/labeled/ready yet")

    return r


def check_downloaded(session: Session) -> CheckResult:
    r = CheckResult("Stage: Downloaded")
    items = fetch_items_by_status(session, JobStatus.DOWNLOADED)

    if not items:
        r.warn("No items in DOWNLOADED state (may all have progressed further)")
        return r

    missing_path = 0
    empty_file = 0
    has_video = 0

    for item in items:
        if not item.file_path:
            missing_path += 1
            continue
        if not os.path.exists(item.file_path):
            empty_file += 1
            continue
        if os.path.getsize(item.file_path) == 0:
            empty_file += 1
            continue
        if item.content_type == ContentType.VIDEO:
            has_video += 1

    checked = len(items)
    good = checked - missing_path - empty_file
    r.ok(f"Checked {checked} downloaded items: {good} valid files on disk")

    if missing_path:
        r.fail(f"{missing_path} items have no file_path set in DB")
    if empty_file:
        r.fail(f"{empty_file} items have missing or empty files on disk")
    if has_video:
        r.warn(
            f"{has_video} items are still VIDEO type — "
            "expected audio after conversion (check AUDIO_DIR vs DOWNLOAD_DIR)"
        )

    return r


def check_transcribed(session: Session) -> CheckResult:
    r = CheckResult("Stage: Transcribed")

    # Include items that have progressed past transcription too
    items = session.execute(
        sa.select(ContentItem).where(
            ContentItem.status.in_([
                JobStatus.TRANSCRIBED, JobStatus.LABELING,
                JobStatus.LABELED, JobStatus.LABEL_FAILED,
                JobStatus.CHUNKING, JobStatus.VECTORIZED, JobStatus.READY,
            ])
        ).limit(200)
    ).scalars().all()

    if not items:
        r.warn("No transcribed items found")
        return r

    no_text = 0
    too_short = 0
    no_cyrillic = 0
    good = 0

    for item in items:
        if not item.transcript_text:
            no_text += 1
            continue
        text = item.transcript_text.strip()
        if len(text) < 50:
            too_short += 1
            continue
        if not CYRILLIC_RE.search(text):
            no_cyrillic += 1
            continue
        good += 1

    r.ok(f"Checked {len(items)} transcribed items: {good} passed all checks")

    if no_text:
        r.fail(f"{no_text} items have empty transcript_text")
    if too_short:
        r.fail(f"{too_short} items have suspiciously short transcripts (<50 chars)")
    if no_cyrillic:
        r.warn(f"{no_cyrillic} items have no Cyrillic — check whisper language setting")

    # Sample display
    sample = next((i for i in items if i.transcript_text and len(i.transcript_text) > 50), None)
    if sample:
        snippet = sample.transcript_text[:200].replace("\n", " ")
        r.details.append(f"  📄 Sample (msg_{sample.source_message_id}): {snippet}…")

    return r


def check_labeled(session: Session) -> CheckResult:
    r = CheckResult("Stage: Labeled")

    items = session.execute(
        sa.select(ContentItem).where(
            ContentItem.status.in_([
                JobStatus.LABELED, JobStatus.CHUNKING,
                JobStatus.VECTORIZED, JobStatus.READY,
            ])
        ).limit(200)
    ).scalars().all()

    if not items:
        r.warn("No labeled items found")
        return r

    no_summary = 0
    no_tags = 0
    no_themes = 0
    bad_category = 0
    good = 0

    tag_vocabulary: set[str] = set()

    for item in items:
        issues = []
        if not item.summary or len(item.summary.strip()) < 20:
            issues.append("summary")
            no_summary += 1
        if not item.tags or len(item.tags) == 0:
            issues.append("tags")
            no_tags += 1
        else:
            tag_vocabulary.update(item.tags)
        if not item.themes or len(item.themes) == 0:
            issues.append("themes")
            no_themes += 1
        if item.content_category and item.content_category not in VALID_CATEGORIES:
            bad_category += 1

        if not issues:
            good += 1

    r.ok(f"Checked {len(items)} labeled items: {good} fully populated")

    if no_summary:
        r.fail(f"{no_summary} items missing summary")
    if no_tags:
        r.fail(f"{no_tags} items have empty tags list")
    if no_themes:
        r.fail(f"{no_themes} items have empty themes list")
    if bad_category:
        r.warn(f"{bad_category} items have unrecognized content_category value")

    if tag_vocabulary:
        sample_tags = ", ".join(sorted(tag_vocabulary)[:15])
        r.details.append(f"  🏷️  Tag vocabulary sample ({len(tag_vocabulary)} unique): {sample_tags}")

    return r


def check_vectorized(chroma_host: str, chroma_port: int, session: Session) -> CheckResult:
    r = CheckResult("Stage: Vectorized (ChromaDB)")

    try:
        import chromadb
        client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
        collections = client.list_collections()
    except Exception as e:
        r.fail(f"Cannot connect to ChromaDB at {chroma_host}:{chroma_port}: {e}")
        return r

    if not collections:
        r.fail("No collections found in ChromaDB")
        return r

    r.ok(f"ChromaDB reachable — {len(collections)} collection(s): {[c.name for c in collections]}")

    for col in collections:
        collection = client.get_collection(col.name)
        count = collection.count()

        if count == 0:
            r.fail(f"Collection '{col.name}' is empty")
            continue

        r.ok(f"Collection '{col.name}': {count} chunks stored")

        # Sample a few chunks and validate metadata
        sample = collection.get(limit=5, include=["documents", "metadatas"])
        bad_meta = 0
        empty_docs = 0

        for doc, meta in zip(sample["documents"], sample["metadatas"]):
            if not doc or len(doc.strip()) < 10:
                empty_docs += 1
            required_keys = {"item_id", "blogger_id", "chunk_index"}
            if not required_keys.issubset(meta.keys()):
                bad_meta += 1

        if empty_docs:
            r.fail(f"'{col.name}': {empty_docs}/5 sampled chunks have empty document text")
        if bad_meta:
            r.fail(f"'{col.name}': {bad_meta}/5 sampled chunks have missing metadata keys")

    # Cross-check: DB says N items are READY, ChromaDB should have ≥ N chunks
    ready_items = fetch_items_by_status(session, JobStatus.READY)
    db_chunk_total = sum(i.chunk_count or 0 for i in ready_items)
    chroma_total = sum(
        client.get_collection(c.name).count() for c in collections
    )

    if db_chunk_total > 0:
        if chroma_total >= db_chunk_total:
            r.ok(f"ChromaDB chunks ({chroma_total}) ≥ expected from DB ({db_chunk_total})")
        else:
            r.fail(
                f"ChromaDB has {chroma_total} chunks but DB expects {db_chunk_total} "
                f"from {len(ready_items)} READY items — possible vectorization gap"
            )

    return r


# ── RAG test ─────────────────────────────────────────────────────────────────

def check_rag(chroma_host: str, chroma_port: int, openai_api_key: str) -> CheckResult:
    r = CheckResult("RAG: Semantic Search Test")

    try:
        import chromadb
        chroma = chromadb.HttpClient(host=chroma_host, port=chroma_port)
        collections = chroma.list_collections()
    except Exception as e:
        r.fail(f"ChromaDB unavailable: {e}")
        return r

    if not collections:
        r.fail("No ChromaDB collections to query")
        return r

    if not openai_api_key:
        r.warn("OPENAI_API_KEY not set — skipping embedding-based RAG test")
        return r

    try:
        from openai import OpenAI
        oai = OpenAI(api_key=openai_api_key)
    except ImportError:
        r.fail("openai package not installed")
        return r

    # Use first available collection
    collection_name = collections[0].name
    collection = chroma.get_collection(collection_name)

    if collection.count() == 0:
        r.fail(f"Collection '{collection_name}' is empty — no data to search")
        return r

    r.ok(f"Querying collection '{collection_name}' ({collection.count()} chunks)")

    for query in RAG_TEST_QUERIES:
        try:
            # Embed the query
            emb_response = oai.embeddings.create(
                model="text-embedding-3-small",
                input=query,
            )
            query_embedding = emb_response.data[0].embedding

            # Query ChromaDB
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=3,
                include=["documents", "metadatas", "distances"],
            )

            docs = results["documents"][0]
            distances = results["distances"][0]
            metas = results["metadatas"][0]

            if not docs:
                r.fail(f"No results for: «{query}»")
                continue

            best_doc = docs[0]
            best_dist = distances[0]
            best_meta = metas[0]

            # similarity = 1 - cosine_distance
            similarity = 1 - best_dist

            has_cyrillic = bool(CYRILLIC_RE.search(best_doc))
            min_similarity = 0.25  # low threshold — content may be varied

            if not has_cyrillic:
                r.fail(f"Best result for «{query[:40]}» has no Cyrillic text")
            elif similarity < min_similarity:
                r.warn(
                    f"Low similarity ({similarity:.2f}) for «{query[:40]}» "
                    f"— content may not cover this topic"
                )
            else:
                r.ok(f"«{query[:45]}»  → sim={similarity:.2f}  chunk_idx={best_meta.get('chunk_index')}")

            snippet = best_doc[:150].replace("\n", " ")
            r.details.append(f"    💬 Best match: {snippet}…")

        except Exception as e:
            r.fail(f"Query failed «{query[:40]}»: {e}")

    return r


# ── Chunking sanity ───────────────────────────────────────────────────────────

def check_chunks_sanity(session: Session) -> CheckResult:
    r = CheckResult("Stage: Chunk Quality")

    items = session.execute(
        sa.select(ContentItem).where(
            ContentItem.status.in_([JobStatus.READY, JobStatus.CHUNKING, JobStatus.VECTORIZED]),
            ContentItem.chunk_count.is_not(None),
        ).limit(100)
    ).scalars().all()

    if not items:
        r.warn("No items with chunk_count set")
        return r

    zero_chunks = 0
    suspicious_chunks = 0
    total_chunks = 0

    for item in items:
        cnt = item.chunk_count or 0
        total_chunks += cnt
        if cnt == 0:
            zero_chunks += 1
        elif cnt > 500:
            suspicious_chunks += 1
            r.warn(f"msg_{item.source_message_id}: {cnt} chunks (very high — check chunking logic)")

        # Validate chunks-per-minute ratio for audio/video
        if item.duration_seconds and cnt > 0 and item.duration_seconds > 0:
            minutes = item.duration_seconds / 60
            chunks_per_min = cnt / minutes
            if chunks_per_min < 0.5:
                r.warn(f"msg_{item.source_message_id}: only {chunks_per_min:.1f} chunks/min — transcript may be truncated")

    good = len(items) - zero_chunks
    r.ok(f"{len(items)} items checked: {good} have chunks, avg {total_chunks / max(len(items), 1):.1f} chunks each")

    if zero_chunks:
        r.fail(f"{zero_chunks} READY items have chunk_count=0")

    return r


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pipeline integrity checker")
    parser.add_argument("--no-rag", action="store_true", help="Skip RAG test (no OpenAI call)")
    parser.add_argument("--stage", choices=["overview", "download", "transcribe", "label", "vector", "chunks", "rag"],
                        help="Run only a specific stage")
    args = parser.parse_args()

    chroma = ChromaSettings()
    db = DatabaseSettings()
    openai_key = os.getenv("OPENAI_API_KEY", "")

    print("=" * 65)
    print("  BLOGER-BOT PIPELINE INTEGRITY CHECK")
    print("=" * 65)
    print(f"  DB:     {db.POSTGRES_HOST}:{db.POSTGRES_PORT}/{db.POSTGRES_DB}")
    print(f"  Chroma: {chroma.CHROMA_HOST}:{chroma.CHROMA_PORT}")
    print(f"  OpenAI: {'✅ key found' if openai_key else '❌ no key (RAG test will be skipped)'}")
    print("=" * 65)

    try:
        engine = make_engine()
        with Session(engine) as session:
            results: list[CheckResult] = []

            only = args.stage

            if not only or only == "overview":
                results.append(check_overview(session))
            if not only or only == "download":
                results.append(check_downloaded(session))
            if not only or only == "transcribe":
                results.append(check_transcribed(session))
            if not only or only == "label":
                results.append(check_labeled(session))
            if not only or only == "chunks":
                results.append(check_chunks_sanity(session))
            if not only or only == "vector":
                results.append(check_vectorized(chroma.CHROMA_HOST, chroma.CHROMA_PORT, session))
            if (not only or only == "rag") and not args.no_rag:
                results.append(check_rag(chroma.CHROMA_HOST, chroma.CHROMA_PORT, openai_key))

            for r in results:
                r.print_summary()

            # Final verdict
            total_failed = sum(r.failed for r in results)
            total_warned = sum(r.warnings for r in results)
            print("\n" + "=" * 65)
            if total_failed == 0:
                print(f"\033[32m  ALL CHECKS PASSED\033[0m  ({total_warned} warnings)")
            else:
                print(f"\033[31m  {total_failed} CHECKS FAILED\033[0m  ({total_warned} warnings)")
                sys.exit(1)

    except Exception as e:
        print(f"\033[31m[FATAL]\033[0m Cannot connect to database: {e}")
        print("Make sure PostgreSQL is running and .env is correct.")
        sys.exit(2)


if __name__ == "__main__":
    main()