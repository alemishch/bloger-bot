"""
Ingest videos from public Yandex Disk folders into the content pipeline.

Downloads one file at a time → converts to audio → transcribes → deletes media.
Stores metadata (yandex URL, original filename, folder) for future library feature.

Usage:
    python tools/ingest_yandex_disk.py --url "https://disk.yandex.ru/d/XXX" --source-name "Готовые видео"
    python tools/ingest_yandex_disk.py --url "https://disk.yandex.ru/d/XXX" --dry-run
    python tools/ingest_yandex_disk.py --url "https://disk.yandex.ru/d/XXX" --skip-existing
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ingestion-service", "src"))

YADISK_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
AUDIO_DIR = Path("data/yadisk_audio")
VIDEO_DIR = Path("data/yadisk_downloads")

WHISPER_MAX_BYTES = 24 * 1024 * 1024


def list_files(public_url: str, path: str = "/") -> list[dict]:
    """List all files recursively from a public Yandex Disk folder."""
    r = requests.get(YADISK_API, params={"public_key": public_url, "path": path, "limit": 200})
    r.raise_for_status()
    data = r.json()

    files = []
    for item in data.get("_embedded", {}).get("items", []):
        if item["type"] == "dir":
            sub = list_files(public_url, f"{path.rstrip('/')}/{item['name']}")
            files.extend(sub)
        elif item["type"] == "file":
            mime = item.get("media_type", "") or item.get("mime_type", "")
            if mime.startswith("video") or item["name"].lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
                files.append({
                    "name": item["name"],
                    "path": f"{path.rstrip('/')}/{item['name']}",
                    "size": item.get("size", 0),
                    "media_type": mime,
                    "modified": item.get("modified", ""),
                })
    return files


def get_download_url(public_url: str, path: str) -> str:
    r = requests.get(f"{YADISK_API}/download", params={"public_key": public_url, "path": path})
    r.raise_for_status()
    return r.json()["href"]


def download_file(url: str, dest: Path, expected_size: int = 0) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        if expected_size and abs(dest.stat().st_size - expected_size) < 1024 * 1024:
            print(f"  ⏭️  Already downloaded: {dest}")
            return True

    print(f"  ⬇️  Downloading {expected_size // 1024 // 1024}MB → {dest}")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r  ⬇️  {downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB ({pct:.0f}%)", end="", flush=True)
        print()
    return dest.exists() and dest.stat().st_size > 0


def convert_to_audio(video_path: Path, audio_path: Path) -> bool:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    if audio_path.exists() and audio_path.stat().st_size > 0:
        print(f"  ⏭️  Audio exists: {audio_path}")
        return True
    print(f"  🔄 Converting to audio...")
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", str(audio_path), "-y"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ❌ ffmpeg failed: {result.stderr[-200:]}")
        return False
    print(f"  ✅ Audio: {audio_path.stat().st_size // 1024 // 1024}MB")
    return True


def split_audio(audio_path: Path) -> list[Path]:
    size = audio_path.stat().st_size
    if size <= WHISPER_MAX_BYTES:
        return [audio_path]

    import math
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
        capture_output=True, text=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    n_chunks = math.ceil(size / WHISPER_MAX_BYTES)
    seg_secs = int(duration / n_chunks) + 1

    pattern = str(audio_path.with_suffix("")) + "_part%03d.mp3"
    subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-f", "segment", "-segment_time", str(seg_secs),
         "-c", "copy", pattern, "-y"],
        capture_output=True, text=True,
    )

    parts = sorted(audio_path.parent.glob(f"{audio_path.stem}_part*.mp3"))
    print(f"  📦 Split into {len(parts)} parts")
    return parts


def transcribe(audio_path: Path, api_key: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    chunks = split_audio(audio_path)
    parts = []
    for i, chunk in enumerate(chunks):
        print(f"  🎤 Transcribing part {i + 1}/{len(chunks)}...")
        with open(chunk, "rb") as f:
            resp = client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
        parts.append(resp.text)
        if chunk != audio_path:
            chunk.unlink(missing_ok=True)

    return "\n".join(parts)


def label_and_vectorize(item_id: str, ingestion_url: str = "http://localhost:8002"):
    """Trigger labeling via the existing pipeline."""
    from ingestion_service.workers.tasks import label_item
    label_item.delay(item_id)
    print(f"  🏷️  Queued for labeling: {item_id}")


def create_source(name: str, public_url: str, blogger_id: str = "yuri") -> str:
    """Create a yandex_disk source via the API."""
    r = requests.post("http://localhost:8002/api/v1/sources/", json={
        "name": name,
        "source_type": "yandex_disk",
        "blogger_id": blogger_id,
        "config": {"public_url": public_url},
    })
    if r.status_code == 200:
        sid = r.json()["id"]
        print(f"✅ Source created: {sid}")
        return sid
    print(f"⚠️  Source creation failed: {r.text}")
    return ""


def upsert_content_item(source_id: str, file_info: dict, transcript: str, blogger_id: str = "yuri") -> str:
    """Insert content item directly into DB."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from common.config import DatabaseSettings

    db = DatabaseSettings()
    engine = sa.create_engine(db.sync_url, echo=False)

    item_id = str(uuid.uuid4())
    with Session(engine) as session:
        existing = session.execute(
            sa.text("SELECT id FROM content_items WHERE source_id = CAST(:sid AS uuid) AND title = :title"),
            {"sid": source_id, "title": file_info["name"]},
        ).first()
        if existing:
            print(f"  ⏭️  Already exists: {file_info['name']}")
            return str(existing[0])

        session.execute(
            sa.text("""
                INSERT INTO content_items
                (id, source_id, content_type, blogger_id, status, title, transcript_text,
                 file_size_bytes, media_type, raw_metadata, created_at, updated_at, retry_count)
                VALUES (CAST(:id AS uuid), CAST(:sid AS uuid),
                        CAST('video' AS contenttype), CAST(:bid AS bloggerid),
                        CAST('transcribed' AS jobstatus),
                        :title, :transcript, :size, :mime, CAST(:meta AS json),
                        NOW(), NOW(), 0)
            """),
            {
                "id": item_id, "sid": source_id, "bid": blogger_id,
                "title": file_info["name"], "transcript": transcript,
                "size": file_info.get("size"), "mime": file_info.get("media_type", "video/mp4"),
                "meta": json.dumps({
                    "source_type": "yandex_disk",
                    "original_file_name": file_info["name"],
                    "original_path": file_info["path"],
                    "file_size_bytes": file_info.get("size"),
                    "yandex_modified": file_info.get("modified", ""),
                }, ensure_ascii=False),
            },
        )
        session.commit()

    return item_id


def main():
    parser = argparse.ArgumentParser(description="Ingest videos from public Yandex Disk folder")
    parser.add_argument("--url", required=True, help="Public Yandex Disk folder URL")
    parser.add_argument("--source-name", default="", help="Name for the content source")
    parser.add_argument("--blogger-id", default="yuri")
    parser.add_argument("--dry-run", action="store_true", help="List files only, don't download")
    parser.add_argument("--skip-existing", action="store_true", help="Skip files already in DB")
    parser.add_argument("--max-files", type=int, default=0, help="Stop after N files (0=all)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        print("❌ OPENAI_API_KEY not set")
        sys.exit(1)

    print(f"\n📂 Scanning: {args.url}")
    files = list_files(args.url)
    print(f"📄 Found {len(files)} video files\n")

    if args.dry_run:
        for f in files:
            print(f"  {f['name']} ({f['size'] // 1024 // 1024}MB)")
        total = sum(f["size"] for f in files)
        print(f"\n  Total: {total // 1024 // 1024 // 1024}GB")
        return

    source_name = args.source_name or f"yandex_disk_{args.url.split('/')[-1][:8]}"

    os.environ.setdefault("POSTGRES_HOST", "localhost")
    source_id = create_source(source_name, args.url, args.blogger_id)
    if not source_id:
        sys.exit(1)

    processed = 0
    for i, file_info in enumerate(files):
        if args.max_files and processed >= args.max_files:
            print(f"\n⏹️  Reached max-files={args.max_files}")
            break

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(files)}] {file_info['name']} ({file_info['size']//1024//1024}MB)")

        video_path = VIDEO_DIR / file_info["name"].replace("/", "_")
        audio_path = AUDIO_DIR / (video_path.stem + ".mp3")

        try:
            # Check if transcript backup already exists (from a previous failed run)
            transcript_backup = Path("data/yadisk_transcripts") / f"{video_path.stem}.txt"
            if transcript_backup.exists() and transcript_backup.stat().st_size > 100:
                print(f"  📄 Using existing transcript backup: {transcript_backup}")
                transcript = transcript_backup.read_text(encoding="utf-8")
                item_id = upsert_content_item(source_id, file_info, transcript, args.blogger_id)
                print(f"  💾 Saved: {item_id}")
                processed += 1
                continue

            dl_url = get_download_url(args.url, file_info["path"])
            if not download_file(dl_url, video_path, file_info["size"]):
                print(f"  ❌ Download failed")
                continue

            if not convert_to_audio(video_path, audio_path):
                continue

            video_path.unlink(missing_ok=True)
            print(f"  🗑️  Deleted video")

            transcript = transcribe(audio_path, api_key)
            print(f"  📝 Transcript: {len(transcript)} chars")

            # Save transcript backup before DB insert (in case DB fails)
            transcript_backup = Path("data/yadisk_transcripts") / f"{video_path.stem}.txt"
            transcript_backup.parent.mkdir(parents=True, exist_ok=True)
            transcript_backup.write_text(transcript, encoding="utf-8")
            print(f"  💾 Transcript backup: {transcript_backup}")

            audio_path.unlink(missing_ok=True)
            print(f"  🗑️  Deleted audio")

            item_id = upsert_content_item(source_id, file_info, transcript, args.blogger_id)
            print(f"  💾 Saved: {item_id}")

            processed += 1

        except Exception as e:
            print(f"  ❌ Error: {e}")
            video_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            if "insufficient_quota" in str(e) or "billing" in str(e).lower():
                print(f"\n💰 OpenAI quota exhausted! Processed {processed}/{len(files)} files.")
                print("Add money to your OpenAI account and re-run with --skip-existing")
                sys.exit(2)
            continue

    print(f"\n✅ Done! Processed {processed}/{len(files)} files from {source_name}")
    print(f"   Now queue labeling: curl -s -X POST 'http://localhost:8002/api/v1/jobs/queue-transcribed?limit=500'")


if __name__ == "__main__":
    main()
