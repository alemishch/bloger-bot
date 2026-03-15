from common.config import BaseAppSettings


class IngestionSettings(BaseAppSettings):
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_NAME: str = "content_parser"
    TELEGRAM_SOURCE_CHANNELS: str = ""
    SESSIONS_DIR: str = "/app/sessions"

    DOWNLOAD_DIR: str = "/app/data/downloads"
    AUDIO_DIR: str = "/app/data/audio"           # converted mp3s live here
    TRANSCRIPTION_OUTPUT_DIR: str = "/app/data/transcriptions"
    LABELED_OUTPUT_DIR: str = "/app/data/labeled"
    EXPORT_DIR: str = "/app/data/exports"

    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    MAX_RETRIES: int = 3

    OPENAI_API_KEY: str = ""

    CHROMA_HOST: str = "chromadb"
    CHROMA_PORT: int = 8000

    @property
    def source_channels(self) -> list[str]:
        return [ch.strip() for ch in self.TELEGRAM_SOURCE_CHANNELS.split(",") if ch.strip()]


settings = IngestionSettings()