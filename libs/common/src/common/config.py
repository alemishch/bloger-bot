from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root_env_file() -> Path | None:
    """``.env`` next to ``docker-compose.dev.yml`` (works no matter the CLI cwd)."""
    p = Path(__file__).resolve().parent
    for _ in range(10):
        if (p / "docker-compose.dev.yml").is_file():
            dotenv = p / ".env"
            return dotenv if dotenv.is_file() else None
        if p.parent == p:
            break
        p = p.parent
    return None


class DatabaseSettings(BaseSettings):
    """Reads env vars; loads repo-root ``.env`` when present (host-run CLIs match Docker Compose)."""

    model_config = SettingsConfigDict(
        env_file=_repo_root_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "bloger_bot"
    POSTGRES_USER: str = "bloger_bot"
    POSTGRES_PASSWORD: str = "changeme"

    @property
    def async_url(self) -> str:
        user = quote_plus(self.POSTGRES_USER)
        pwd = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{pwd}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_url(self) -> str:
        user = quote_plus(self.POSTGRES_USER)
        pwd = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql://{user}:{pwd}"
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