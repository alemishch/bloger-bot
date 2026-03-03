"""
Thin DB client for the transcriber tool.
Uses sync SQLAlchemy since Whisper blocks the thread anyway.
"""
import uuid
from sqlalchemy import create_engine, select, update, text
from sqlalchemy.orm import Session
from transcriber.config import settings

engine = create_engine(settings.sync_db_url, echo=False)


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
                "file_path": row.file_path,
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
                SET status = 'transcribed',
                    transcript_path = :path,
                    transcript_text = :text,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": item_id, "path": transcript_path, "text": transcript_text},
        )
        session.commit()


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