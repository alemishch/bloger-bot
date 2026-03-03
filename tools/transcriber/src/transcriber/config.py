from pydantic_settings import BaseSettings


class TranscriberSettings(BaseSettings):
    WHISPER_MODEL: str = "large-v3"
    WHISPER_DEVICE: str = "cuda"
    WHISPER_COMPUTE_TYPE: str = "float16"
    WHISPER_LANGUAGE: str = "ru"

    DOWNLOAD_DIR: str = "./data/downloads"
    TRANSCRIPTION_OUTPUT_DIR: str = "./data/transcriptions"

    # Database connection for updating job status
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "bloger_bot"
    POSTGRES_USER: str = "bloger_bot"
    POSTGRES_PASSWORD: str = "changeme"

    # Ingestion service API (alternative to direct DB)
    INGESTION_API_URL: str = "http://localhost:8002"

    BATCH_SIZE: int = 10
    WATCH_INTERVAL: int = 30  # seconds

    class Config:
        env_file = ".env"

    @property
    def async_db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_db_url(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = TranscriberSettings()