from celery import Celery
from ingestion_service.config import settings

celery_app = Celery(
    "ingestion_service",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=7200,
    task_time_limit=10800,
    task_default_queue="default",
    task_routes={
        "download_media_batch": {"queue": "downloads"},
        "download_media": {"queue": "downloads"},
        "parse_telegram_channel": {"queue": "default"},
        "parse_channel_text": {"queue": "default"},
        "convert_and_transcribe": {"queue": "transcriptions"},
        "label_item": {"queue": "default"},
        "vectorize_item": {"queue": "default"},
    },
)

celery_app.autodiscover_tasks(["ingestion_service.workers"])