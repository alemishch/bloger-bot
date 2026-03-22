import asyncio
import json
import math
import os
import subprocess
import structlog
from typing import Optional

from ingestion_service.workers.celery_app import celery_app
from ingestion_service.config import settings

logger = structlog.get_logger()

WHISPER_MAX_BYTES = 24 * 1024 * 1024  # 24 MB


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)


def make_session_factory():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from common.config import DatabaseSettings
    db = DatabaseSettings()
    engine = create_async_engine(
        db.async_url,
        echo=False,
        pool_size=3,
        max_overflow=5,
        connect_args={"prepared_statement_cache_size": 0},
    )
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


def _audio_path_for_item(source_message_id: int) -> str:
    """Canonical audio path. Single source of truth — used everywhere."""
    return os.path.join(settings.AUDIO_DIR, f"msg_{source_message_id}.mp3")


# ── Audio helpers ────────────────────────────────────────────────────────────

def _sync_convert_to_audio(input_path: str, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-i", input_path,
            "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k",
            output_path, "-y",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-800:]}")
    return output_path


def _sync_get_duration(audio_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
        capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _sync_split_audio(audio_path: str) -> list[str]:
    file_size = os.path.getsize(audio_path)
    if file_size <= WHISPER_MAX_BYTES:
        return [audio_path]

    duration = _sync_get_duration(audio_path)
    n_chunks = math.ceil(file_size / WHISPER_MAX_BYTES)
    segment_secs = int(duration / n_chunks) + 1
    basename = os.path.splitext(audio_path)[0]
    chunk_pattern = f"{basename}_part%03d.mp3"

    result = subprocess.run(
        ["ffmpeg", "-i", audio_path, "-f", "segment", "-segment_time", str(segment_secs),
         "-c", "copy", chunk_pattern, "-y"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg split failed:\n{result.stderr[-800:]}")

    dir_ = os.path.dirname(audio_path)
    prefix = os.path.basename(basename) + "_part"
    return sorted(os.path.join(dir_, f) for f in os.listdir(dir_)
                  if f.startswith(prefix) and f.endswith(".mp3"))


# ── Task 1: Parse ────────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="parse_telegram_channel", max_retries=3)
def parse_telegram_channel(self, source_id: str):
    async def _parse():
        from ingestion_service.telegram_client import get_client
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import ContentType, JobStatus
        import uuid

        session_factory, engine = make_session_factory()
        try:
            async with session_factory() as session:
                jm = JobManager(session)
                source = await jm.get_source(uuid.UUID(source_id))
                if not source:
                    raise ValueError(f"Source {source_id} not found")

                chat_id = source.config.get("channel_id") or source.config.get("chat_id")
                if not chat_id:
                    raise ValueError("No channel_id/chat_id in source config")

                client = await get_client()
                messages = []
                async for message in client.get_chat_history(int(chat_id), limit=500):
                    if source.last_parsed_message_id and message.id <= source.last_parsed_message_id:
                        break
                    messages.append(message)

                logger.info("parse_fetched", count=len(messages), source_id=source_id)
                count = 0
                media_item_ids = []
                text_item_ids = []
                max_message_id = source.last_parsed_message_id or 0

                for message in reversed(messages):
                    item_data = _message_to_parsed_item(message)
                    if not item_data:
                        continue
                    try:
                        ct = ContentType(item_data["content_type"])
                    except ValueError:
                        ct = ContentType.TEXT

                    item = await jm.upsert_content_item(
                        source_id=source.id,
                        source_message_id=item_data["source_message_id"],
                        content_type=ct,
                        blogger_id=source.blogger_id,
                        text=item_data.get("text"),
                        media_type=item_data.get("media_type"),
                        duration_seconds=item_data.get("duration_seconds"),
                        file_size_bytes=item_data.get("file_size_bytes"),
                        date=item_data.get("date"),
                        raw_metadata=item_data.get("raw_metadata", {}),
                        title=item_data.get("title"),
                    )
                    count += 1
                    if message.id > max_message_id:
                        max_message_id = message.id
                    if ct in (ContentType.VIDEO, ContentType.AUDIO):
                        media_item_ids.append(str(item.id))
                    elif ct in (ContentType.TEXT, ContentType.POST) and item_data.get("text"):
                        text = item_data["text"].strip()
                        if len(text) >= 30 and item.status == JobStatus.DISCOVERED:
                            await jm.update_item_status(
                                item.id, JobStatus.TRANSCRIBED,
                                transcript_text=text,
                            )
                            text_item_ids.append(str(item.id))

                if max_message_id > (source.last_parsed_message_id or 0):
                    await jm.update_last_parsed_message_id(source.id, max_message_id)

                if media_item_ids:
                    download_media_batch.delay(media_item_ids)
                    logger.info("batch_download_queued", count=len(media_item_ids))

                if text_item_ids:
                    for tid in text_item_ids:
                        label_item.delay(tid)
                    logger.info("text_label_queued", count=len(text_item_ids))

                return {"parsed_count": count, "media_queued": len(media_item_ids),
                        "text_queued": len(text_item_ids)}
        finally:
            await engine.dispose()

    return run_async(_parse())


# ── Task 1b: Parse channel text posts (background, cancellable) ──────────────

@celery_app.task(bind=True, name="parse_channel_text", max_retries=1,
                 soft_time_limit=3600, time_limit=3900)
def parse_channel_text(self, source_id: str, batch_size: int = 200, max_messages: int = 0):
    """
    Parse text posts from a Telegram channel in batches.
    Runs in background; cancel via: celery_app.control.revoke(task_id, terminate=True)
    max_messages=0 means unlimited.
    """
    async def _parse():
        from ingestion_service.telegram_client import get_client
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import ContentType, JobStatus
        import uuid

        session_factory, engine = make_session_factory()
        try:
            async with session_factory() as session:
                jm = JobManager(session)
                source = await jm.get_source(uuid.UUID(source_id))
                if not source:
                    raise ValueError(f"Source {source_id} not found")

                chat_id = source.config.get("channel_id") or source.config.get("chat_id")
                if not chat_id:
                    raise ValueError("No channel_id/chat_id in source config")

                client = await get_client()
                total_parsed = 0
                total_text_queued = 0
                max_message_id = source.last_parsed_message_id or 0
                offset_id = 0
                done = False

                while not done:
                    messages = []
                    async for message in client.get_chat_history(
                        chat_id if isinstance(chat_id, int) else chat_id,
                        limit=batch_size,
                        offset_id=offset_id,
                    ):
                        if source.last_parsed_message_id and message.id <= source.last_parsed_message_id:
                            done = True
                            break
                        messages.append(message)

                    if not messages:
                        break

                    offset_id = messages[-1].id

                    for message in reversed(messages):
                        item_data = _message_to_parsed_item(message)
                        if not item_data:
                            continue

                        try:
                            ct = ContentType(item_data["content_type"])
                        except ValueError:
                            ct = ContentType.TEXT

                        if ct not in (ContentType.TEXT, ContentType.POST):
                            continue
                        text = (item_data.get("text") or "").strip()
                        if len(text) < 30:
                            continue

                        item = await jm.upsert_content_item(
                            source_id=source.id,
                            source_message_id=item_data["source_message_id"],
                            content_type=ct,
                            blogger_id=source.blogger_id,
                            text=text,
                            date=item_data.get("date"),
                            raw_metadata=item_data.get("raw_metadata", {}),
                        )
                        total_parsed += 1

                        if message.id > max_message_id:
                            max_message_id = message.id

                        if item.status == JobStatus.DISCOVERED:
                            await jm.update_item_status(
                                item.id, JobStatus.TRANSCRIBED,
                                transcript_text=text,
                            )
                            label_item.delay(str(item.id))
                            total_text_queued += 1

                    logger.info("parse_channel_text_batch",
                                source_id=source_id, batch_parsed=len(messages),
                                total_parsed=total_parsed, total_queued=total_text_queued)

                    if max_messages and total_parsed >= max_messages:
                        break

                    self.update_state(state="PROGRESS", meta={
                        "total_parsed": total_parsed,
                        "total_queued": total_text_queued,
                    })

                if max_message_id > (source.last_parsed_message_id or 0):
                    await jm.update_last_parsed_message_id(source.id, max_message_id)

                logger.info("parse_channel_text_done",
                            source_id=source_id,
                            total_parsed=total_parsed,
                            total_queued=total_text_queued)
                return {"total_parsed": total_parsed, "total_queued": total_text_queued}
        finally:
            await engine.dispose()

    return run_async(_parse())


# ── Task 2: Batch download ───────────────────────────────────────────────────

@celery_app.task(bind=True, name="download_media_batch", max_retries=2)
def download_media_batch(self, item_ids: list[str]):
    async def _batch():
        from ingestion_service.telegram_client import get_client
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import JobStatus
        import uuid

        session_factory, engine = make_session_factory()
        try:
            client = await get_client()
            done = 0
            failed = 0

            async with session_factory() as session:
                jm = JobManager(session)
                for item_id in item_ids:
                    try:
                        item = await jm.get_item(uuid.UUID(item_id))
                        if not item:
                            continue

                        # ── Audio already exists? Skip download entirely ──
                        audio_path = _audio_path_for_item(item.source_message_id)
                        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                            logger.info("audio_exists_skip_download", item_id=item_id, path=audio_path)
                            await jm.update_item_status(
                                item.id, JobStatus.DOWNLOADED, file_path=audio_path
                            )
                            convert_and_transcribe.delay(item_id)
                            done += 1
                            continue

                        # ── Video already downloaded? ──
                        if (item.file_path and os.path.exists(item.file_path)
                                and os.path.getsize(item.file_path) > 0):
                            logger.info("file_exists_skip_download", item_id=item_id)
                            convert_and_transcribe.delay(item_id)
                            done += 1
                            continue

                        source = await jm.get_source(item.source_id)
                        chat_id = int(source.config.get("channel_id") or source.config.get("chat_id"))
                        await jm.update_item_status(item.id, JobStatus.DOWNLOADING)

                        ext = _mime_to_ext(item.media_type or "")
                        filename = f"msg_{item.source_message_id}{ext}"
                        filepath = os.path.join(settings.DOWNLOAD_DIR, filename)
                        os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)

                        message = await client.get_messages(chat_id, item.source_message_id)
                        await client.download_media(message, file_name=filepath)

                        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                            await jm.update_item_status(item.id, JobStatus.DOWNLOADED, file_path=filepath)
                            convert_and_transcribe.delay(item_id)
                            logger.info("download_ok", path=filepath)
                            done += 1
                        else:
                            await jm.update_item_status(item.id, JobStatus.DOWNLOAD_FAILED,
                                                         error_message="Empty file after download")
                            failed += 1

                    except Exception as e:
                        logger.error("download_item_failed", item_id=item_id, error=str(e))
                        try:
                            async with session_factory() as err_session:
                                await JobManager(err_session).update_item_status(
                                    uuid.UUID(item_id), JobStatus.DOWNLOAD_FAILED, error_message=str(e)
                                )
                        except Exception:
                            pass
                        failed += 1

            logger.info("batch_done", done=done, failed=failed, total=len(item_ids))
            return {"done": done, "failed": failed}
        finally:
            await engine.dispose()

    return run_async(_batch())


@celery_app.task(bind=True, name="download_media", max_retries=3)
def download_media(self, content_item_id: str):
    download_media_batch.delay([content_item_id])
    return {"queued": content_item_id}


# ── Task 3: Convert + Transcribe ─────────────────────────────────────────────

@celery_app.task(bind=True, name="convert_and_transcribe", max_retries=2)
def convert_and_transcribe(self, content_item_id: str):
    """
    1. If audio already exists at canonical path → skip conversion.
    2. Otherwise convert video → audio, update file_path, delete video.
    3. Split if > 24 MB.
    4. Transcribe via Groq Whisper (cheap) or OpenAI Whisper (fallback).
    5. Save transcript, queue label_item.
    """

    async def _run():
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import JobStatus, ContentType
        import uuid

        session_factory, engine = make_session_factory()
        try:
            async with session_factory() as session:
                jm = JobManager(session)
                item = await jm.get_item(uuid.UUID(content_item_id))
                if not item:
                    return

                await jm.update_item_status(item.id, JobStatus.TRANSCRIBING)

                audio_path = _audio_path_for_item(item.source_message_id)

                # ── Step A: Get or create audio file ─────────────────────
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                    logger.info("audio_already_exists", path=audio_path)
                else:
                    source_path = item.file_path
                    if not source_path or not os.path.exists(source_path):
                        logger.error("source_file_missing", path=source_path, item_id=content_item_id)
                        await jm.update_item_status(
                            item.id, JobStatus.DOWNLOAD_FAILED,
                            error_message=f"Source file missing: {source_path}. Must re-download."
                        )
                        return

                    # If source is the same file as target audio — skip conversion
                    if os.path.abspath(source_path) == os.path.abspath(audio_path):
                        logger.info("source_is_audio_same_path", path=source_path)
                        # Ensure DB points to the canonical audio path so retries find it
                        await jm.update_item_status(item.id, JobStatus.DOWNLOADED, file_path=audio_path)
                    else:
                        logger.info("converting_to_audio", src=source_path, dst=audio_path)
                        try:
                            await asyncio.to_thread(_sync_convert_to_audio, source_path, audio_path)
                        except Exception as e:
                            await jm.update_item_status(item.id, JobStatus.TRANSCRIPTION_FAILED,
                                                        error_message=f"Conversion failed: {e}")
                            raise self.retry(exc=e, countdown=60)

                        # Preserve original file name in raw_metadata for deduplication
                        meta = item.raw_metadata or {}
                        if "original_file_name" not in meta:
                            meta["original_file_name"] = os.path.basename(source_path)
                            meta["original_file_path"] = source_path
                            await jm.update_item_status(
                                item.id, JobStatus.TRANSCRIBING,
                                file_path=audio_path,
                                raw_metadata=meta,
                            )
                        else:
                            await jm.update_item_status(item.id, JobStatus.TRANSCRIBING, file_path=audio_path)

                        # Delete original video to save space
                        if item.content_type == ContentType.VIDEO and os.path.exists(source_path):
                            try:
                                os.remove(source_path)
                                logger.info("video_deleted", path=source_path)
                            except OSError as e:
                                logger.warning("video_delete_failed", error=str(e))

                # ── Step B: Split if needed ───────────────────────────────
                try:
                    chunks = await asyncio.to_thread(_sync_split_audio, audio_path)
                except Exception as e:
                    await jm.update_item_status(item.id, JobStatus.TRANSCRIPTION_FAILED,
                                                 error_message=f"Split failed: {e}")
                    raise self.retry(exc=e, countdown=60)

                # ── Step C: Transcribe ────────────────────────────────────
                try:
                    transcript_parts = await _transcribe_chunks(chunks)
                except Exception as e:
                    await jm.update_item_status(item.id, JobStatus.TRANSCRIPTION_FAILED,
                                                 error_message=str(e))
                    raise self.retry(exc=e, countdown=30 * (self.request.retries + 1))
                finally:
                    # Clean up split chunks (keep the main audio file)
                    for chunk_path in chunks:
                        if chunk_path != audio_path and os.path.exists(chunk_path):
                            try:
                                os.remove(chunk_path)
                            except OSError:
                                pass

                full_transcript = "\n".join(transcript_parts)

                await jm.update_item_status(
                    item.id, JobStatus.TRANSCRIBED,
                    transcript_text=full_transcript,
                    file_path=audio_path,
                )
                logger.info("transcription_complete", item_id=content_item_id,
                            chunks=len(chunks), chars=len(full_transcript))

                # Delete audio file after transcript is safely saved to DB
                if os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                        logger.info("audio_deleted_after_transcription", path=audio_path)
                    except OSError as e:
                        logger.warning("audio_delete_failed", error=str(e))

                label_item.delay(content_item_id)
                return {"chars": len(full_transcript), "chunks": len(chunks)}
        finally:
            await engine.dispose()

    return run_async(_run())


async def _transcribe_chunks(chunks: list[str]) -> list[str]:
    """
    Transcribe using Groq Whisper (primary — ~18x cheaper than OpenAI).
    Falls back to OpenAI Whisper if Groq is not configured.
    """
    groq_key = getattr(settings, "GROQ_API_KEY", None)

    if groq_key:
        return await _transcribe_with_groq(chunks, groq_key)
    else:
        return await _transcribe_with_openai(chunks, settings.OPENAI_API_KEY)


async def _transcribe_with_groq(chunks: list[str], api_key: str) -> list[str]:
    """
    Groq Whisper: $0.02/hour (OpenAI charges $0.36/hour — 18x cheaper).
    Model: whisper-large-v3-turbo (faster + slightly cheaper than large-v3).
    """
    from groq import AsyncGroq

    client = AsyncGroq(api_key=api_key)
    parts = []
    for i, chunk_path in enumerate(chunks):
        logger.info("transcribing_chunk_groq", chunk=i + 1, total=len(chunks))
        with open(chunk_path, "rb") as f:
            response = await client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=f,
                language="ru",
                response_format="text",
            )
        parts.append(response if isinstance(response, str) else response.text)
    return parts


async def _transcribe_with_openai(chunks: list[str], api_key: str) -> list[str]:
    from openai import AsyncOpenAI
    async with AsyncOpenAI(api_key=api_key) as client:
        parts = []
        for i, chunk_path in enumerate(chunks):
            logger.info("transcribing_chunk_openai", chunk=i + 1, total=len(chunks))
            with open(chunk_path, "rb") as f:
                response = await client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="ru",
                )
            parts.append(response.text)
    return parts


# ── Task 4: LLM labeling ─────────────────────────────────────────────────────

@celery_app.task(bind=True, name="label_item", max_retries=2)
def label_item(self, content_item_id: str):
    async def _label():
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import JobStatus
        import uuid

        session_factory, engine = make_session_factory()
        try:
            async with session_factory() as session:
                jm = JobManager(session)
                item = await jm.get_item(uuid.UUID(content_item_id))
                source_text = item.transcript_text or item.text if item else None
                if not item or not source_text:
                    logger.warning("label_skip_no_text", item_id=content_item_id)
                    return

                if item.status != JobStatus.TRANSCRIBED:
                    logger.info(
                        "label_skip_wrong_status",
                        item_id=content_item_id,
                        status=item.status.value if item.status else None,
                    )
                    return

                await jm.update_item_status(item.id, JobStatus.LABELING)

                prompt = f"""Проведи анализ русского текста и верни ТОЛЬКО JSON с полями:
- summary: строка (2–3 предложения)
- tags: список строк (5–10 ключевых слов)
- themes: список строк (основные темы)
- problems_solved: список строк
- tools_mentioned: список строк
- target_audience: строка
- content_category: строка (education/motivational/case_study/product_review/personal_story)
- is_paid: булево

Текст:
{source_text[:4000]}

Только валидный JSON на русском языке."""

                import openai
                async with openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY) as client:
                    response = await client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        temperature=0.3,
                    )

                result = json.loads(response.choices[0].message.content)
                def safe_str(s: str, maxlen: int) -> str:
                    if s is None:
                        return None
                    s = s.strip()
                    return s if len(s) <= maxlen else s[:maxlen]

                target_audience = safe_str(result.get("target_audience"), 64)
                content_category = safe_str(result.get("content_category"), 64)

                # optionally ensure tags/themes are lists
                tags = result.get("tags") or []
                themes = result.get("themes") or []

                await jm.update_item_status(
                    item.id, JobStatus.LABELED,
                    summary=result.get("summary"),
                    tags=tags,
                    themes=themes,
                    problems_solved=result.get("problems_solved", []),
                    tools_mentioned=result.get("tools_mentioned", []),
                    target_audience=target_audience,
                    content_category=content_category,
                    is_paid=bool(result.get("is_paid", False)),
                )
                logger.info("label_complete", item_id=content_item_id)
                vectorize_item.delay(content_item_id)
                return result
        finally:
            await engine.dispose()

    return run_async(_label())


# ── Task 5: Vectorize ────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="vectorize_item", max_retries=2)
def vectorize_item(self, content_item_id: str):
    async def _vectorize():
        from ingestion_service.services.job_manager import JobManager
        from common.models.enums import JobStatus
        import uuid
        import chromadb
        from openai import AsyncOpenAI

        session_factory, engine = make_session_factory()
        try:
            async with session_factory() as session:
                jm = JobManager(session)
                item = await jm.get_item(uuid.UUID(content_item_id))
                source_text = item.transcript_text or item.text if item else None
                if not item or not source_text:
                    return

                await jm.update_item_status(item.id, JobStatus.CHUNKING)
                chunks = _chunk_text(source_text, chunk_size=500, overlap=50)

                if not chunks:
                    logger.error("no_chunks_produced", item_id=content_item_id)
                    await jm.update_item_status(item.id, JobStatus.LABEL_FAILED,
                                                 error_message="No chunks produced from transcript")
                    return

                chroma = chromadb.HttpClient(
                    host=getattr(settings, "CHROMA_HOST", "chromadb"),
                    port=int(getattr(settings, "CHROMA_PORT", 8000)),
                )
                collection = chroma.get_or_create_collection(
                    name=f"blogger_{item.blogger_id.value}",
                )

                # Embed in batches of 50 — avoids token limit errors on large transcripts
                EMBED_BATCH = 50
                all_embeddings = []
                async with AsyncOpenAI(api_key=settings.OPENAI_API_KEY) as openai_client:
                    for batch_start in range(0, len(chunks), EMBED_BATCH):
                        batch = chunks[batch_start:batch_start + EMBED_BATCH]
                        resp = await openai_client.embeddings.create(
                            model="text-embedding-3-small",
                            input=batch,
                        )
                        all_embeddings.extend(e.embedding for e in resp.data)

                ids = [f"{content_item_id}_chunk_{i}" for i in range(len(chunks))]
                metadatas = [
                    {
                        "item_id": content_item_id,
                        "blogger_id": item.blogger_id.value,
                        "chunk_index": i,
                        "source_message_id": item.source_message_id or 0,
                        "content_type": item.content_type.value,
                        "tags": ",".join(item.tags or []),
                        "summary": (item.summary or "")[:500],
                    }
                    for i in range(len(chunks))
                ]
                collection.upsert(ids=ids, embeddings=all_embeddings, documents=chunks, metadatas=metadatas)

                await jm.update_item_status(
                    item.id, JobStatus.READY,
                    chroma_collection=f"blogger_{item.blogger_id.value}",
                    chunk_count=len(chunks),
                )
                logger.info("vectorize_complete", item_id=content_item_id, chunks=len(chunks))
                return {"chunks": len(chunks)}
        finally:
            await engine.dispose()

    return run_async(_vectorize())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _message_to_parsed_item(message) -> Optional[dict]:
    base = {
        "source_message_id": message.id,
        "text": message.text or message.caption or "",
        "date": message.date,
        "content_type": "text",
        "raw_metadata": {
            "views": getattr(message, "views", None),
            "forwards": getattr(message, "forwards", None),
        },
    }
    if not message.media:
        return base if message.text else None
    if message.video:
        v = message.video
        return {**base, "content_type": "video", "media_type": v.mime_type or "video/mp4",
                "duration_seconds": float(v.duration or 0), "file_size_bytes": v.file_size,
                "title": v.file_name}
    if message.video_note:
        vn = message.video_note
        return {**base, "content_type": "video", "media_type": "video/mp4",
                "duration_seconds": float(vn.duration or 0), "file_size_bytes": vn.file_size}
    if message.audio:
        a = message.audio
        return {**base, "content_type": "audio", "media_type": a.mime_type or "audio/mpeg",
                "duration_seconds": float(a.duration or 0), "file_size_bytes": a.file_size,
                "title": a.title or a.file_name}
    if message.voice:
        vc = message.voice
        return {**base, "content_type": "audio", "media_type": vc.mime_type or "audio/ogg",
                "duration_seconds": float(vc.duration or 0), "file_size_bytes": vc.file_size}
    if message.document:
        doc = message.document
        mime = doc.mime_type or ""
        if mime.startswith("video/") or mime.startswith("audio/"):
            ct = "video" if mime.startswith("video/") else "audio"
            return {**base, "content_type": ct, "media_type": mime,
                    "file_size_bytes": doc.file_size, "title": doc.file_name}
    return None


def _mime_to_ext(mime_type: str) -> str:
    return {
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
        "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
        "audio/opus": ".opus", "audio/mp4": ".m4a", "audio/aac": ".aac",
    }.get(mime_type, ".mp4")


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    # ~6000 chars ≈ 3000 tokens for Russian — safely within embedding model's 8192 limit
    HARD_MAX_CHARS = 6000

    try:
        from razdel import sentenize
        sents = [s.text for s in sentenize(text)]
    except Exception:
        import re
        sents = re.split(r'(?<=[.!?…])\s+', text.strip())

    chunks, current, cur_len = [], [], 0
    for s in sents:
        if cur_len + len(s) > chunk_size and current:
            chunks.append(" ".join(current))
            overlap_sents, overlap_len = [], 0
            for ss in reversed(current):
                if overlap_len + len(ss) <= overlap:
                    overlap_sents.insert(0, ss)
                    overlap_len += len(ss)
                else:
                    break
            current, cur_len = overlap_sents, overlap_len
        current.append(s)
        cur_len += len(s)
    if current:
        chunks.append(" ".join(current))

    # Hard safety pass: split any chunk that exceeds the embedding token limit
    safe_chunks = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        if len(chunk) <= HARD_MAX_CHARS:
            safe_chunks.append(chunk)
        else:
            for i in range(0, len(chunk), HARD_MAX_CHARS):
                part = chunk[i:i + HARD_MAX_CHARS].strip()
                if part:
                    safe_chunks.append(part)

    return safe_chunks