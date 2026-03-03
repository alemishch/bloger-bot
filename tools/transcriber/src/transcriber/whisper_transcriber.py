import json
import os
import time
from faster_whisper import WhisperModel
from transcriber.config import settings


class WhisperTranscriber:
    def __init__(self):
        self.model = WhisperModel(
            settings.WHISPER_MODEL,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
        )

    def transcribe(self, audio_path: str) -> dict:
        started = time.time()
        segments, info = self.model.transcribe(
            audio_path,
            language=settings.WHISPER_LANGUAGE,
            vad_filter=True,
        )
        rows = []
        full_text = []
        for s in segments:
            rows.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
            full_text.append(s.text.strip())

        return {
            "file_path": audio_path,
            "language": info.language,
            "duration": round(info.duration, 2),
            "processing_time": round(time.time() - started, 2),
            "full_text": " ".join(full_text),
            "segments": rows,
        }

    @staticmethod
    def save(result: dict, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(result["file_path"]))[0]
        out = os.path.join(output_dir, f"{name}_transcript.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return out