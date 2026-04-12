import os
import yaml
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    OPENAI_API_KEY: str = ""
    CHROMA_HOST: str = "chromadb"
    CHROMA_PORT: int = 8000
    BLOGGER_ID: str = "yuri"
    CONFIG_DIR: str = "/app/config/bloggers"
    CHAT_MODEL: str = "gpt-4o-mini"
    OPENAI_STAGE_MODEL: str = "gpt-4o-mini"
    # If set, used for analysis / hypothesis / rerank / judge instead of OPENAI_STAGE_MODEL (reasoning-capable models).
    OPENAI_REASONING_MODEL: str = ""
    EMBED_MODEL: str = "text-embedding-3-small"
    # httpx timeouts for all OpenAI API calls (embeddings often hit this first)
    OPENAI_HTTP_TIMEOUT: float = 300.0
    OPENAI_HTTP_CONNECT_TIMEOUT: float = 60.0


settings = LLMSettings()


def load_blogger_config(blogger_id: str | None = None) -> dict:
    bid = blogger_id or settings.BLOGGER_ID
    config_path = Path(settings.CONFIG_DIR) / f"{bid}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Blogger config not found: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    expanded = os.path.expandvars(raw)
    return yaml.safe_load(expanded)
