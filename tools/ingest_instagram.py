"""
Ingest content from a public Instagram profile:
- Reels: download video → audio → transcribe → save
- Posts: save caption text (skip short captions < 100 chars)

Uses instaloader for metadata, yt-dlp for reel video download.
Saves all metadata (shortcode, url, timestamp, likes) for future library.

Usage:
    python tools/ingest_instagram.py --username kinashyuriy --dry-run
    python tools/ingest_instagram.py --username kinashyuriy --skip-existing
    python tools/ingest_instagram.py --username kinashyuriy --reels-only
    python tools/ingest_instagram.py --username kinashyuriy --posts-only
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))

import requests

AUDIO_DIR = Path("data/instagram_audio")
TRANSCRIPTS_DIR = Path("data/instagram_transcripts")
WHISPER_MAX_BYTES = 24 * 1024 * 1024
MIN_CAPTION_LENGTH = 100


def list_posts(username: str) -> tuple[list[dict], list[dict]]:
    """List reels and text posts from public Instagram profile using instaloader."""
    import instaloader
    L = instaloader.Instaloader(
        download_videos=False, download_video_thumbnails=False,
        download_geotags=False, download_comments=False,
        save_metadata=False, compress_json=False,
        quiet=True,
    )

    profile = instaloader.Profile.from_username(L.context, username)
    reels = []
    posts = []

    for post in profile.get_posts():
        meta = {
            "shortcode": post.shortcode,
            "url": f"https://www.instagram.com/p/{post.shortcode}/",
            "timestamp": post.date_utc.isoformat(),
            "likes": post.likes,
            "comments": post.comments,
            "caption": post.caption or "",
            "is_video": post.is_video,
            "video_duration": getattr(post, "video_duration", None),
            "typename": post.typename,
        }

        if post.is_video and post.typename in ("GraphVideo",) and (getattr(post, "video_duration", 0) or 0) > 5:
            reels.append(meta)
        elif not post.is_video and len(meta["caption"]) >= MIN_CAPTION_LENGTH:
            posts.append(meta)

    return reels, posts


def download_reel_audio(shortcode: str, output_dir: Path) -> Path | None:
    """Download reel audio via yt-dlp."""
    import yt_dlp
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{shortcode}.mp3"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    url = f"https://www.instagram.com/reel/{shortcode}/"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{shortcode}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "32"}],
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"  ❌ Reel download failed: {e}")
    return None


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


def create_source(name: str, source_type: str, username: str, blogger_id: str = "yuri") -> str:
    r = requests.post("http://localhost:8002/api/v1/sources/", json={
        "name": name, "source_type": source_type, "blogger_id": blogger_id,
        "config": {"instagram_username": username, "instagram_url": f"https://www.instagram.com/{username}/"},
    })
    if r.status_code == 200:
        sid = r.json()["id"]
        print(f"✅ Source '{name}' created: {sid}")
        return sid
    print(f"⚠️  {r.text}")
    return ""


def upsert_item(source_id: str, content_type: str, title: str, text: str, meta: dict, blogger_id: str = "yuri") -> str:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from common.config import DatabaseSettings

    engine = sa.create_engine(DatabaseSettings().sync_url)
    item_id = str(uuid.uuid4())
    with Session(engine) as session:
        existing = session.execute(
            sa.text("SELECT id FROM content_items WHERE source_id = CAST(:sid AS uuid) AND title = :title"),
            {"sid": source_id, "title": title},
        ).first()
        if existing:
            return str(existing[0])

        ct = "video" if content_type == "reel" else "post"
        session.execute(sa.text("""
            INSERT INTO content_items
            (id, source_id, content_type, blogger_id, status, title, transcript_text, text,
             source_url, duration_seconds, raw_metadata, created_at, updated_at, retry_count)
            VALUES (CAST(:id AS uuid), CAST(:sid AS uuid),
                    CAST(:ct AS contenttype), CAST(:bid AS bloggerid),
                    CAST('transcribed' AS jobstatus),
                    :title, :transcript, :text, :url, :dur,
                    CAST(:meta AS json), NOW(), NOW(), 0)
        """), {
            "id": item_id, "sid": source_id, "bid": blogger_id, "ct": ct,
            "title": title, "transcript": text, "text": text,
            "url": meta.get("url", ""), "dur": meta.get("video_duration"),
            "meta": json.dumps(meta, ensure_ascii=False, default=str),
        })
        session.commit()
    return item_id


def main():
    parser = argparse.ArgumentParser(description="Ingest Instagram reels + post text")
    parser.add_argument("--username", required=True)
    parser.add_argument("--blogger-id", default="yuri")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--reels-only", action="store_true")
    parser.add_argument("--posts-only", action="store_true")
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run and not args.posts_only:
        print("❌ OPENAI_API_KEY needed for reel transcription"); sys.exit(1)

    print(f"\n📸 Scanning Instagram: @{args.username}")
    print("  (this may take a minute for large profiles...)")
    reels, posts = list_posts(args.username)
    print(f"  🎬 Reels: {len(reels)}")
    print(f"  📝 Posts (≥{MIN_CAPTION_LENGTH} chars): {len(posts)}\n")

    if args.dry_run:
        if not args.posts_only:
            print("=== REELS ===")
            for r in reels[:10]:
                print(f"  {r['shortcode']} | {r['caption'][:50]}... | {r.get('video_duration',0)}s | ❤️{r['likes']}")
            if len(reels) > 10:
                print(f"  ... and {len(reels)-10} more")
        if not args.reels_only:
            print("\n=== POSTS ===")
            for p in posts[:10]:
                print(f"  {p['shortcode']} | {p['caption'][:60]}... | ❤️{p['likes']}")
            if len(posts) > 10:
                print(f"  ... and {len(posts)-10} more")
        return

    os.environ.setdefault("POSTGRES_HOST", "localhost")

    # ── Process posts (text only, no transcription needed) ──
    if not args.reels_only and posts:
        post_source_id = create_source("inst_post", "instagram_post", args.username, args.blogger_id)
        if post_source_id:
            saved = 0
            for i, p in enumerate(posts):
                title = f"ig_post_{p['shortcode']}"
                item_id = upsert_item(post_source_id, "post", title, p["caption"], p, args.blogger_id)
                saved += 1
            print(f"  💾 Saved {saved} Instagram posts\n")

    # ── Process reels (download + transcribe) ──
    if not args.posts_only and reels:
        reel_source_id = create_source("reels", "instagram_reels", args.username, args.blogger_id)
        if reel_source_id:
            processed = 0
            for i, r in enumerate(reels):
                if args.max_files and processed >= args.max_files:
                    break
                print(f"\n[{i+1}/{len(reels)}] Reel {r['shortcode']} ({r.get('video_duration',0)}s)")

                transcript_file = TRANSCRIPTS_DIR / f"{r['shortcode']}.txt"
                try:
                    if transcript_file.exists() and transcript_file.stat().st_size > 50:
                        transcript = transcript_file.read_text(encoding="utf-8")
                        print(f"  📄 Using backup")
                    else:
                        audio = download_reel_audio(r["shortcode"], AUDIO_DIR)
                        if not audio:
                            continue
                        transcript = transcribe(audio, api_key)
                        transcript_file.parent.mkdir(parents=True, exist_ok=True)
                        transcript_file.write_text(transcript, encoding="utf-8")
                        audio.unlink(missing_ok=True)
                        for f in AUDIO_DIR.glob(f"{r['shortcode']}*"):
                            f.unlink(missing_ok=True)

                    full_text = transcript
                    if r["caption"]:
                        full_text = f"{r['caption']}\n\n---\n\n{transcript}"

                    title = f"ig_reel_{r['shortcode']}"
                    upsert_item(reel_source_id, "reel", title, full_text, r, args.blogger_id)
                    print(f"  💾 Saved ({len(transcript)} chars)")
                    processed += 1

                except Exception as e:
                    print(f"  ❌ {e}")
                    if "insufficient_quota" in str(e):
                        print(f"\n💰 Quota exhausted. Re-run with --skip-existing")
                        sys.exit(2)

            print(f"\n✅ Reels done: {processed}/{len(reels)}")

    print(f"\n✅ Instagram ingestion complete!")
    print(f"   Queue labeling: curl -s -X POST 'http://localhost:8002/api/v1/jobs/queue-transcribed?limit=500'")


if __name__ == "__main__":
    main()
