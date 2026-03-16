import os
import yaml
from pathlib import Path
from pydantic_settings import BaseSettings


class BotSettings(BaseSettings):
    BLOGGER_ID: str = "yuri"
    CONFIG_DIR: str = "/app/config/bloggers"
    LLM_SERVICE_URL: str = "http://llm-service:8000"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = ""
    POSTGRES_USER: str = ""
    POSTGRES_PASSWORD: str = ""

    class Config:
        env_file = ".env"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = BotSettings()


def load_blogger_config(blogger_id: str | None = None) -> dict:
    bid = blogger_id or settings.BLOGGER_ID
    config_path = Path(settings.CONFIG_DIR) / f"{bid}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Blogger config not found: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    expanded = os.path.expandvars(raw)
    return yaml.safe_load(expanded)
