import os
import yaml
from pathlib import Path
from pydantic_settings import BaseSettings


class LLMSettings(BaseSettings):
    OPENAI_API_KEY: str = ""
    CHROMA_HOST: str = "chromadb"
    CHROMA_PORT: int = 8000
    BLOGGER_ID: str = "yuri"
    CONFIG_DIR: str = "/app/config/bloggers"
    CHAT_MODEL: str = "gpt-4o-mini"
    EMBED_MODEL: str = "text-embedding-3-small"

    class Config:
        env_file = ".env"


settings = LLMSettings()


def load_blogger_config(blogger_id: str | None = None) -> dict:
    bid = blogger_id or settings.BLOGGER_ID
    config_path = Path(settings.CONFIG_DIR) / f"{bid}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Blogger config not found: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    expanded = os.path.expandvars(raw)
    return yaml.safe_load(expanded)
