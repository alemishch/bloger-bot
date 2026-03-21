import os
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from common.database import get_session
from common.models.content import ContentItem
from common.models.enums import JobStatus, ContentType
from ingestion_service.services.job_manager import JobManager

router = APIRouter()


@router.get("/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    jm = JobManager(session)
    return await jm.get_pipeline_stats()


@router.get("/")
async def list_items(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=500),
    session: AsyncSession = Depends(get_session),
):
    if status:
        try:
            job_status = JobStatus(status.lower())
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid status. Valid: {[s.value for s in JobStatus]}")
    else:
        job_status = None

    jm = JobManager(session)
    if job_status:
        items = await jm.get_items_by_status(job_status, limit=limit)
    else:
        q = select(ContentItem).order_by(ContentItem.created_at.desc()).limit(limit)
        result = await session.execute(q)
        items = list(result.scalars().all())

    return [
        {
            "id": str(item.id),
            "source_message_id": item.source_message_id,
            "content_type": item.content_type.value if item.content_type else None,
            "status": item.status.value if item.status else None,
            "file_path": item.file_path,
            "error_message": item.error_message,
            "retry_count": item.retry_count,
            "duration_seconds": item.duration_seconds,
            "created_at": str(item.created_at),
        }
        for item in items
    ]


@router.post("/recover-all")
async def recover_all(session: AsyncSession = Depends(get_session)):
    """
    Single endpoint that recovers ALL items that are stuck or failed.

    Handles:
    - stuck in-progress states (chunking, labeling, transcribing, downloading)
      → reset to previous completed state and re-queue
    - *_failed states
      → check files on disk to decide cheapest recovery path
    """
    from ingestion_service.workers.tasks import (
        download_media_batch, convert_and_transcribe, label_item, vectorize_item,
        _audio_path_for_item,
    )

    # States that are "in progress" but nobody is processing them (worker died)
    stuck_in_progress = [
        JobStatus.DOWNLOADING,
        JobStatus.TRANSCRIBING,
        JobStatus.LABELING,
        JobStatus.CHUNKING,
    ]
    failed_states = [
        JobStatus.DOWNLOAD_FAILED,
        JobStatus.TRANSCRIPTION_FAILED,
        JobStatus.LABEL_FAILED,
    ]

    q = select(ContentItem).where(
        ContentItem.status.in_(stuck_in_progress + failed_states)
    ).order_by(ContentItem.created_at)
    result = await session.execute(q)
    items = list(result.scalars().all())

    jm = JobManager(session)
    report = {
        "total": len(items),
        "queued_vectorize": [],    # chunking → vectorize
        "queued_label": [],        # labeling/transcribed → label
        "queued_transcribe": [],   # transcribing/downloaded → transcribe (audio exists)
        "queued_download": [],     # transcribing (no audio) + downloading + download_failed
        "skipped": [],             # nothing to do
    }

    to_download_ids = []

    for item in items:
        msg_id = item.source_message_id
        audio_path = _audio_path_for_item(msg_id) if msg_id else None
        audio_exists = bool(audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0)
        source_file_exists = bool(
            item.file_path and os.path.exists(item.file_path) and os.path.getsize(item.file_path) > 0
        )

        # ── Stuck: chunking ──────────────────────────────────────────────
        if item.status == JobStatus.CHUNKING:
            # Has transcript → just redo vectorization
            if item.transcript_text:
                await jm.update_item_status(item.id, JobStatus.LABELED, error_message=None)
                vectorize_item.delay(str(item.id))
                report["queued_vectorize"].append(str(item.id))
            else:
                # Transcript is gone — fall through to label recovery
                item.status = JobStatus.TRANSCRIPTION_FAILED  # treat as failed

        # ── Stuck: labeling ──────────────────────────────────────────────
        if item.status == JobStatus.LABELING:
            if item.transcript_text:
                await jm.update_item_status(item.id, JobStatus.TRANSCRIBED, error_message=None)
                label_item.delay(str(item.id))
                report["queued_label"].append(str(item.id))
            else:
                item.status = JobStatus.TRANSCRIPTION_FAILED

        # ── Stuck: transcribing ──────────────────────────────────────────
        if item.status == JobStatus.TRANSCRIBING:
            if audio_exists:
                await jm.update_item_status(item.id, JobStatus.DOWNLOADED,
                                             error_message=None, file_path=audio_path)
                convert_and_transcribe.delay(str(item.id))
                report["queued_transcribe"].append(str(item.id))
            else:
                # Audio never got created — re-download
                await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
                to_download_ids.append(str(item.id))
                report["queued_download"].append(str(item.id))

        # ── Stuck: downloading ───────────────────────────────────────────
        elif item.status == JobStatus.DOWNLOADING:
            await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
            to_download_ids.append(str(item.id))
            report["queued_download"].append(str(item.id))

        # ── Failed: label ────────────────────────────────────────────────
        elif item.status == JobStatus.LABEL_FAILED:
            if item.transcript_text:
                await jm.update_item_status(item.id, JobStatus.TRANSCRIBED, error_message=None)
                label_item.delay(str(item.id))
                report["queued_label"].append(str(item.id))
            else:
                item.status = JobStatus.TRANSCRIPTION_FAILED

        # ── Failed: transcription ────────────────────────────────────────
        if item.status == JobStatus.TRANSCRIPTION_FAILED:
            if audio_exists:
                # Audio file is there — just retry transcription (e.g. API was down)
                await jm.update_item_status(item.id, JobStatus.DOWNLOADED,
                                             error_message=None, file_path=audio_path)
                convert_and_transcribe.delay(str(item.id))
                report["queued_transcribe"].append(str(item.id))
            else:
                # No audio. Check if source video is corrupt by looking at error message
                error = item.error_message or ""
                is_corrupt = any(s in error for s in [
                    "moov atom not found",
                    "Invalid data found",
                    "Invalid argument",
                    "No such file",
                ])
                if is_corrupt and source_file_exists:
                    # Delete corrupt file, re-download
                    try:
                        os.remove(item.file_path)
                    except OSError:
                        pass
                await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
                to_download_ids.append(str(item.id))
                report["queued_download"].append(str(item.id))

        # ── Failed: download ─────────────────────────────────────────────
        elif item.status == JobStatus.DOWNLOAD_FAILED:
            if audio_exists:
                await jm.update_item_status(item.id, JobStatus.DOWNLOADED,
                                             error_message=None, file_path=audio_path)
                convert_and_transcribe.delay(str(item.id))
                report["queued_transcribe"].append(str(item.id))
            else:
                await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
                to_download_ids.append(str(item.id))
                report["queued_download"].append(str(item.id))

    # Queue all downloads as one batch task (single Pyrogram client)
    if to_download_ids:
        download_media_batch.delay(to_download_ids)

    # Deduplicate report lists (an item can only be in one list)
    for key in ("queued_vectorize", "queued_label", "queued_transcribe", "queued_download"):
        report[key] = list(dict.fromkeys(report[key]))  # preserve order, deduplicate
        report[f"{key}_count"] = len(report[key])
        del report[key]  # replace list with count to keep response readable

    return report


# Keep backwards compat alias
@router.post("/retry-all-failed")
async def retry_all_failed(session: AsyncSession = Depends(get_session)):
    return await recover_all(session)


@router.post("/{item_id}/retry")
async def retry_item(item_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    from ingestion_service.workers.tasks import (
        download_media_batch, convert_and_transcribe, label_item, vectorize_item,
        _audio_path_for_item,
    )

    jm = JobManager(session)
    item = await jm.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    audio_path = _audio_path_for_item(item.source_message_id) if item.source_message_id else None
    audio_exists = bool(audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0)

    if item.status in (JobStatus.CHUNKING, JobStatus.LABELED):
        await jm.update_item_status(item.id, JobStatus.LABELED, error_message=None)
        vectorize_item.delay(str(item_id))
        return {"item_id": str(item_id), "action": "queued_for_vectorization"}

    if item.status in (JobStatus.LABELING, JobStatus.LABEL_FAILED, JobStatus.TRANSCRIBED):
        await jm.update_item_status(item.id, JobStatus.TRANSCRIBED, error_message=None)
        label_item.delay(str(item_id))
        return {"item_id": str(item_id), "action": "queued_for_labeling"}

    if item.status in (JobStatus.TRANSCRIBING, JobStatus.TRANSCRIPTION_FAILED, JobStatus.DOWNLOADED):
        if audio_exists:
            await jm.update_item_status(item.id, JobStatus.DOWNLOADED,
                                         error_message=None, file_path=audio_path)
            convert_and_transcribe.delay(str(item_id))
            return {"item_id": str(item_id), "action": "queued_for_transcription",
                    "note": "audio found, skipped re-download"}
        await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
        download_media_batch.delay([str(item_id)])
        return {"item_id": str(item_id), "action": "queued_for_download"}

    if item.status in (JobStatus.DOWNLOADING, JobStatus.DOWNLOAD_FAILED, JobStatus.DISCOVERED):
        if audio_exists:
            await jm.update_item_status(item.id, JobStatus.DOWNLOADED,
                                         error_message=None, file_path=audio_path)
            convert_and_transcribe.delay(str(item_id))
            return {"item_id": str(item_id), "action": "queued_for_transcription",
                    "note": "audio found, skipped re-download"}
        await jm.update_item_status(item.id, JobStatus.DISCOVERED, error_message=None)
        download_media_batch.delay([str(item_id)])
        return {"item_id": str(item_id), "action": "queued_for_download"}

    return {"item_id": str(item_id), "action": "no_action", "status": item.status.value}


@router.post("/queue-discovered")
async def queue_discovered_for_download(
    limit: int = Query(500, le=1000),
    session: AsyncSession = Depends(get_session),
):
    from ingestion_service.workers.tasks import download_media_batch
    q = (
        select(ContentItem)
        .where(ContentItem.status == JobStatus.DISCOVERED,
               ContentItem.content_type.in_([ContentType.VIDEO, ContentType.AUDIO]))
        .order_by(ContentItem.created_at)
        .limit(limit)
    )
    result = await session.execute(q)
    items = list(result.scalars().all())
    if not items:
        return {"queued": 0}
    download_media_batch.delay([str(i.id) for i in items])
    return {"queued": len(items)}


@router.post("/queue-downloaded")
async def queue_downloaded_for_transcription(
    limit: int = Query(500, le=1000),
    session: AsyncSession = Depends(get_session),
):
    from ingestion_service.workers.tasks import convert_and_transcribe
    q = (
        select(ContentItem)
        .where(ContentItem.status == JobStatus.DOWNLOADED,
               ContentItem.content_type.in_([ContentType.VIDEO, ContentType.AUDIO]))
        .order_by(ContentItem.created_at)
        .limit(limit)
    )
    result = await session.execute(q)
    items = list(result.scalars().all())
    for item in items:
        convert_and_transcribe.delay(str(item.id))
    return {"queued": len(items)}


@router.post("/queue-transcribed")
async def queue_transcribed_for_labeling(
    limit: int = Query(500, le=1000),
    session: AsyncSession = Depends(get_session),
):
    from ingestion_service.workers.tasks import label_item
    q = (
        select(ContentItem)
        .where(ContentItem.status == JobStatus.TRANSCRIBED)
        .order_by(ContentItem.created_at)
        .limit(limit)
    )
    result = await session.execute(q)
    items = list(result.scalars().all())
    for item in items:
        label_item.delay(str(item.id))
    return {"queued": len(items)}


@router.post("/{item_id}/label")
async def trigger_label(item_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    from ingestion_service.workers.tasks import label_item
    jm = JobManager(session)
    item = await jm.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    label_item.delay(str(item_id))
    return {"item_id": str(item_id), "status": "label_queued"}

@router.post("/queue-labeled")
async def queue_labeled_for_vectorization(
    limit: int = Query(1000, le=5000),
    session: AsyncSession = Depends(get_session),
):
    """Queue all labeled items for vectorization. Also resets stuck chunking items."""
    from ingestion_service.workers.tasks import vectorize_item

    # Reset stuck chunking → labeled
    stuck = await session.execute(
        select(ContentItem).where(ContentItem.status == JobStatus.CHUNKING)
    )
    stuck_items = list(stuck.scalars().all())
    for item in stuck_items:
        item.status = JobStatus.LABELED
        item.updated_at = __import__("datetime").datetime.utcnow()
    await session.commit()

    # Queue all labeled
    q = (
        select(ContentItem)
        .where(ContentItem.status == JobStatus.LABELED)
        .order_by(ContentItem.created_at)
        .limit(limit)
    )
    result = await session.execute(q)
    items = list(result.scalars().all())
    for item in items:
        vectorize_item.delay(str(item.id))
    return {"reset_chunking": len(stuck_items), "queued": len(items)}
