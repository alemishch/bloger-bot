import enum


class ContentType(str, enum.Enum):
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    POST = "post"


class SourceType(str, enum.Enum):
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"
    PDF = "pdf"
    YANDEX_DISK = "yandex_disk"


class JobStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    DOWNLOAD_FAILED = "download_failed"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    TRANSCRIPTION_FAILED = "transcription_failed"
    LABELING = "labeling"
    LABELED = "labeled"
    LABEL_FAILED = "label_failed"
    CHUNKING = "chunking"
    VECTORIZED = "vectorized"
    READY = "ready"
    FAILED = "failed"


class BloggerID(str, enum.Enum):
    YURI = "yuri"
    MARIA = "maria"


class OnboardingStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"