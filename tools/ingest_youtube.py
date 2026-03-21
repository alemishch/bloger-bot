"""
Ingest videos from a YouTube channel into the content pipeline.
Uses yt-dlp for metadata + audio download, OpenAI Whisper for transcription.
Skips shorts (< 90 seconds). Saves all metadata for future library.

Usage:
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy"
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy" --dry-run
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy" --skip-existing --max-files 5
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))

import yt_dlp
import requests

AUDIO_DIR = Path("data/youtube_audio")
TRANSCRIPTS_DIR = Path("data/youtube_transcripts")
WHISPER_MAX_BYTES = 24 * 1024 * 1024
MIN_DURATION = 90  # skip shorts


def list_videos(channel_url: str) -> list[dict]:
    """List all non-short videos from a YouTube channel."""
    ydl_opts = {"quiet": True, "extract_flat": True, "playlistend": 500}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"{channel_url}/videos", download=False)
    
    videos = []
    for entry in info.get("entries", []):
        dur = entry.get("duration") or 0
        if dur < MIN_DURATION:
            continue
        videos.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "duration": dur,
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
        })
    return videos


def download_audio(video_id: str, output_dir: Path, cookies_file: str | None = None) -> Path | None:
    """Download audio-only from YouTube video."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{video_id}.mp3"
    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"  ⏭️  Audio exists: {output_path}")
        return output_path

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "32"}],
        "quiet": True,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        if output_path.exists():
            return output_path
        for f in output_dir.glob(f"{video_id}.*"):
            if f.suffix == ".mp3":
                return f
    except Exception as e:
        print(f"  ❌ Download failed: {e}")
    return None


def get_video_metadata(video_id: str) -> dict:
    """Get full metadata for a single video."""
    ydl_opts = {"quiet": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return {
            "youtube_id": info.get("id"),
            "title": info.get("title", ""),
            "description": (info.get("description") or "")[:2000],
            "duration": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "channel": info.get("channel"),
            "url": info.get("webpage_url"),
            "thumbnail": info.get("thumbnail"),
        }
    except Exception:
        return {"youtube_id": video_id}


def split_audio(audio_path: Path) -> list[Path]:
    import math
    size = audio_path.stat().st_size
    if size <= WHISPER_MAX_BYTES:
        return [audio_path]
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
        capture_output=True, text=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    n = math.ceil(size / WHISPER_MAX_BYTES)
    seg = int(duration / n) + 1
    pattern = str(audio_path.with_suffix("")) + "_part%03d.mp3"
    subprocess.run(["ffmpeg", "-i", str(audio_path), "-f", "segment", "-segment_time", str(seg),
                     "-c", "copy", pattern, "-y"], capture_output=True)
    return sorted(audio_path.parent.glob(f"{audio_path.stem}_part*.mp3"))


def transcribe(audio_path: Path, api_key: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    chunks = split_audio(audio_path)
    parts = []
    for i, chunk in enumerate(chunks):
        print(f"  🎤 Transcribing {i+1}/{len(chunks)}...")
        with open(chunk, "rb") as f:
            resp = client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
        parts.append(resp.text)
        if chunk != audio_path:
            chunk.unlink(missing_ok=True)
    return "\n".join(parts)


def create_source(name: str, channel_url: str, blogger_id: str = "yuri") -> str:
    r = requests.post("http://localhost:8002/api/v1/sources/", json={
        "name": name, "source_type": "youtube", "blogger_id": blogger_id,
        "config": {"channel_url": channel_url},
    })
    if r.status_code == 200:
        sid = r.json()["id"]
        print(f"✅ Source created: {sid}")
        return sid
    print(f"⚠️  Source creation: {r.text}")
    return ""


def upsert_item(source_id: str, video: dict, transcript: str, metadata: dict, blogger_id: str = "yuri") -> str:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from common.config import DatabaseSettings

    engine = sa.create_engine(DatabaseSettings().sync_url)
    item_id = str(uuid.uuid4())
    with Session(engine) as session:
        existing = session.execute(
            sa.text("SELECT id FROM content_items WHERE source_id = CAST(:sid AS uuid) AND title = :title"),
            {"sid": source_id, "title": video["title"]},
        ).first()
        if existing:
            print(f"  ⏭️  Exists: {video['title']}")
            return str(existing[0])

        meta = {
            "source_type": "youtube",
            "youtube_id": video["id"],
            "youtube_url": video["url"],
            "original_title": video["title"],
            "duration_seconds": video["duration"],
            **{k: v for k, v in metadata.items() if k not in ("title",)},
        }
        session.execute(sa.text("""
            INSERT INTO content_items
            (id, source_id, content_type, blogger_id, status, title, transcript_text,
             duration_seconds, source_url, raw_metadata, created_at, updated_at, retry_count)
            VALUES (CAST(:id AS uuid), CAST(:sid AS uuid),
                    CAST('video' AS contenttype), CAST(:bid AS bloggerid),
                    CAST('transcribed' AS jobstatus),
                    :title, :transcript, :dur, :url, CAST(:meta AS json), NOW(), NOW(), 0)
        """), {
            "id": item_id, "sid": source_id, "bid": blogger_id,
            "title": video["title"], "transcript": transcript,
            "dur": video["duration"], "url": video["url"],
            "meta": json.dumps(meta, ensure_ascii=False),
        })
        session.commit()
    return item_id


def main():
    parser = argparse.ArgumentParser(description="Ingest YouTube channel videos")
    parser.add_argument("--channel", required=True, help="YouTube channel URL")
    parser.add_argument("--source-name", default="youtube")
    parser.add_argument("--blogger-id", default="yuri")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--cookies", default=None, help="Path to cookies.txt (Netscape format). Export from browser with 'Get cookies.txt' extension")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        print("❌ OPENAI_API_KEY not set"); sys.exit(1)

    print(f"\n📺 Scanning: {args.channel}")
    videos = list_videos(args.channel)
    print(f"📄 Found {len(videos)} videos (≥{MIN_DURATION}s)\n")

    if args.dry_run:
        for v in videos:
            m = v["duration"] // 60
            print(f"  {v['id']} | {v['title'][:60]} | {m}min")
        print(f"\n  Total: {len(videos)} videos, {sum(v['duration'] for v in videos)//3600}h")
        return

    os.environ.setdefault("POSTGRES_HOST", "localhost")
    source_id = create_source(args.source_name, args.channel, args.blogger_id)
    if not source_id:
        sys.exit(1)

    processed = 0
    for i, video in enumerate(videos):
        if args.max_files and processed >= args.max_files:
            break
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(videos)}] {video['title'][:70]} ({video['duration']//60}min)")

        transcript_file = TRANSCRIPTS_DIR / f"{video['id']}.txt"
        try:
            if transcript_file.exists() and transcript_file.stat().st_size > 100:
                print(f"  📄 Using transcript backup")
                transcript = transcript_file.read_text(encoding="utf-8")
            else:
                audio = download_audio(video["id"], AUDIO_DIR, args.cookies)
                if not audio:
                    continue
                transcript = transcribe(audio, api_key)
                transcript_file.parent.mkdir(parents=True, exist_ok=True)
                transcript_file.write_text(transcript, encoding="utf-8")
                print(f"  💾 Backup: {transcript_file}")
                audio.unlink(missing_ok=True)
                for f in AUDIO_DIR.glob(f"{video['id']}*"):
                    f.unlink(missing_ok=True)

            print(f"  📝 {len(transcript)} chars")
            meta = get_video_metadata(video["id"])
            item_id = upsert_item(source_id, video, transcript, meta, args.blogger_id)
            print(f"  💾 Saved: {item_id}")
            processed += 1

        except Exception as e:
            print(f"  ❌ Error: {e}")
            if "insufficient_quota" in str(e) or "billing" in str(e).lower():
                print(f"\n💰 OpenAI quota exhausted after {processed} files. Re-run with --skip-existing")
                sys.exit(2)

    print(f"\n✅ Done! {processed}/{len(videos)} videos from {args.source_name}")
    print(f"   Queue labeling: curl -s -X POST 'http://localhost:8002/api/v1/jobs/queue-transcribed?limit=500'")


if __name__ == "__main__":
    main()
