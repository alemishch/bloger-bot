from pydantic_settings import BaseSettings
from common.config import BaseAppSettings


class IngestionSettings(BaseAppSettings):
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_NAME: str = "content_parser"
    TELEGRAM_SOURCE_CHANNELS: str = ""  # comma-separated

    DOWNLOAD_DIR: str = "/app/data/downloads"
    TRANSCRIPTION_OUTPUT_DIR: str = "/app/data/transcriptions"

    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    @property
    def source_channels(self) -> list[str]:
        return [ch.strip() for ch in self.TELEGRAM_SOURCE_CHANNELS.split(",") if ch.strip()]


settings = IngestionSettings()