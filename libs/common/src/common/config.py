from pydantic_settings import BaseSettings
from functools import lru_cache


class DatabaseSettings(BaseSettings):
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "bloger_bot"
    POSTGRES_USER: str = "bloger_bot"
    POSTGRES_PASSWORD: str = "changeme"

    @property
    def async_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_url(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


class RedisSettings(BaseSettings):
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = "changeme"

    @property
    def url(self) -> str:
        return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"


class ChromaSettings(BaseSettings):
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000

    @property
    def url(self) -> str:
        return f"http://{self.CHROMA_HOST}:{self.CHROMA_PORT}"


class BaseAppSettings(BaseSettings):
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "DEBUG"
    OPENAI_API_KEY: str = ""

    db: DatabaseSettings = DatabaseSettings()
    redis: RedisSettings = RedisSettings()
    chroma: ChromaSettings = ChromaSettings()

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"