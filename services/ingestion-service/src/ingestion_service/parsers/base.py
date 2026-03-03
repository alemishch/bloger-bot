from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, AsyncIterator


@dataclass
class ParsedItem:
    source_message_id: Optional[int] = None
    source_url: Optional[str] = None
    content_type: str = "text"
    title: Optional[str] = None
    text: Optional[str] = None
    file_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    media_type: Optional[str] = None
    date: Optional[datetime] = None
    raw_metadata: dict = field(default_factory=dict)


class BaseParser(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def parse(self, since_message_id: Optional[int] = None, limit: Optional[int] = None) -> AsyncIterator[ParsedItem]: ...

    @abstractmethod
    async def download_media(self, item: ParsedItem, output_dir: str) -> str: ...