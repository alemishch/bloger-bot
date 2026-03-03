from common.config import BaseAppSettings


class IngestionSettings(BaseAppSettings):
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_NAME: str = "content_parser"
    # Comma-separated: chat IDs (int) or usernames (@handle)
    TELEGRAM_SOURCE_CHANNELS: str = ""

    # Where .session files live (mounted from host)
    SESSIONS_DIR: str = "/app/sessions"

    DOWNLOAD_DIR: str = "/app/data/downloads"
    TRANSCRIPTION_OUTPUT_DIR: str = "/app/data/transcriptions"
    LABELED_OUTPUT_DIR: str = "/app/data/labeled"
    EXPORT_DIR: str = "/app/data/exports"

    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    MAX_RETRIES: int = 3

    @property
    def source_channels(self) -> list[str]:
        return [ch.strip() for ch in self.TELEGRAM_SOURCE_CHANNELS.split(",") if ch.strip()]


settings = IngestionSettings()