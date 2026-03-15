"""
Thin DB client for the transcriber tool.
Uses sync SQLAlchemy since Whisper blocks the thread anyway.
"""
import os
import uuid
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from transcriber.config import settings

engine = create_engine(settings.sync_db_url, echo=False)


def _remap_path(docker_path: str) -> str:
    """
    Convert Docker container path to local path.
    Docker: /app/data/downloads/msg_123.mp4
    Local:  ./data/downloads/msg_123.mp4
    """
    if not docker_path:
        return docker_path
    # Strip known container prefixes
    for prefix in ["/app/data/", "/app/"]:
        if docker_path.startswith(prefix):
            local = os.path.join("./data", docker_path[len(prefix):])
            return os.path.normpath(local)
    return docker_path


def get_downloaded_items(limit: int = 10) -> list[dict]:
    """Fetch content items with status='downloaded' that need transcription."""
    with Session(engine) as session:
        result = session.execute(
            text("""
                SELECT id, file_path, content_type, source_message_id, blogger_id
                FROM content_items
                WHERE status = 'downloaded'
                  AND content_type IN ('video', 'audio')
                  AND file_path IS NOT NULL
                ORDER BY created_at ASC
                LIMIT :limit
            """),
            {"limit": limit},
        )
        rows = result.fetchall()
        return [
            {
                "id": str(row.id),
                "file_path": _remap_path(row.file_path),   # ← fix path
                "content_type": row.content_type,
                "source_message_id": row.source_message_id,
                "blogger_id": row.blogger_id,
            }
            for row in rows
        ]


def update_item_transcribing(item_id: str):
    with Session(engine) as session:
        session.execute(
            text("UPDATE content_items SET status = 'transcribing', updated_at = NOW() WHERE id = :id"),
            {"id": item_id},
        )
        session.commit()


def update_item_transcribed(item_id: str, transcript_path: str, transcript_text: str):
    with Session(engine) as session:
        session.execute(
            text("""
                UPDATE content_items 
                SET status='transcribed', transcript_path=:path, 
                    transcript_text=:text, updated_at=NOW()
                WHERE id=:id
            """),
            {"id": item_id, "path": transcript_path, "text": transcript_text},
        )
        session.commit()

    # Trigger labeling via API (non-blocking)
    try:
        import httpx
        httpx.post(
            f"{settings.INGESTION_API_URL}/api/v1/jobs/{item_id}/label",
            timeout=5,
        )
    except Exception:
        pass


def update_item_transcription_failed(item_id: str, error: str):
    with Session(engine) as session:
        session.execute(
            text("""
                UPDATE content_items
                SET status = 'transcription_failed',
                    error_message = :error,
                    retry_count = retry_count + 1,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": item_id, "error": error},
        )
        session.commit()


def reset_failed_transcriptions(limit: int = 100):
    """Reset transcription_failed items back to downloaded for retry."""
    with Session(engine) as session:
        result = session.execute(
            text("""
                UPDATE content_items
                SET status = 'downloaded',
                    error_message = NULL,
                    updated_at = NOW()
                WHERE status = 'transcription_failed'
                  AND file_path IS NOT NULL
                  AND retry_count < 3
                RETURNING id, file_path
            """),
        )
        rows = result.fetchall()
        session.commit()
        return [{"id": str(r.id), "file_path": r.file_path} for r in rows]


def reset_stuck_transcribing():
    """Reset items stuck in 'transcribing' (e.g. after crash) back to downloaded."""
    with Session(engine) as session:
        session.execute(
            text("""
                UPDATE content_items
                SET status = 'downloaded', updated_at = NOW()
                WHERE status = 'transcribing'
                  AND updated_at < NOW() - INTERVAL '10 minutes'
            """),
        )
        session.commit()