import os
import yaml
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import ConfigDict


class BotSettings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")

    BLOGGER_ID: str = "yuri"
    CONFIG_DIR: str = "/app/config/bloggers"
    LLM_SERVICE_URL: str = "http://llm-service:8000"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = ""
    POSTGRES_USER: str = ""
    POSTGRES_PASSWORD: str = ""
    AMOCRM_ENABLED: bool = False
    AMOCRM_DOMAIN: str = ""
    AMOCRM_ACCESS_TOKEN: str = ""
    AMOCRM_REFRESH_TOKEN: str = ""
    AMOCRM_CLIENT_ID: str = ""
    AMOCRM_CLIENT_SECRET: str = ""
    AMOCRM_REDIRECT_URI: str = ""
    OUTBOUND_PROXY_URL: str = ""
    HTTPS_PROXY: str = ""
    HTTP_PROXY: str = ""

    @property
    def telegram_proxy_url(self) -> str | None:
        """HTTP(S) proxy for Telegram Bot API (aiogram does not read env vars by default)."""
        for raw in (self.OUTBOUND_PROXY_URL, self.HTTPS_PROXY, self.HTTP_PROXY):
            url = (raw or "").strip()
            if url:
                return url
        return None

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
