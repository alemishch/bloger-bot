"""
Ingest videos from a YouTube channel into the content pipeline.
Uses yt-dlp for metadata + audio download, OpenAI Whisper for transcription.
Skips shorts (< 90 seconds). Saves all metadata for future library.

Recommended cookie flow (avoids PO-token / "only images" yt-dlp failures):

1. ``pip install playwright && playwright install chromium``
2. ``python tools/youtube_refresh_cookies.py`` → writes ``cookies/youtube.txt``
3. ``python tools/ingest_youtube.py --channel "https://www.youtube.com/@user" --skip-existing``

Install **Node.js** or **Deno** so yt-dlp can solve YouTube's n-challenge (EJS).
Optional: ``set YT_DLP_EJS_NPM=1`` before ingest to allow ``ejs:npm`` remote components.

Usage:
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy"
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy" --dry-run
    python tools/ingest_youtube.py --channel "https://www.youtube.com/@kinashyuriy" --skip-existing --max-files 5
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))

import yt_dlp
import requests

from ingest_common import ingestion_api_base_url, queue_transcribed_for_labeling

AUDIO_DIR = Path("data/youtube_audio")
TRANSCRIPTS_DIR = Path("data/youtube_transcripts")
WHISPER_MAX_BYTES = 24 * 1024 * 1024
MIN_DURATION = 90  # skip shorts


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _hydrate_openai_key_from_dotenv() -> None:
    """Load OPENAI_API_KEY from repo ``.env`` if not already in the environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    path = _repo_root() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k == "OPENAI_API_KEY" and v:
            os.environ["OPENAI_API_KEY"] = v
            return


def _js_runtimes() -> dict:
    """Enable Node (preferred) or Deno for YouTube n-challenge / EJS; yt-dlp defaults to Deno-only."""
    runtimes: dict = {}
    for exe in ("node", "deno", "bun"):
        if shutil.which(exe):
            runtimes[exe] = {}
    if not runtimes:
        runtimes["deno"] = {}
    return runtimes


def _youtube_ydl_opts(
    *,
    quiet: bool = True,
    extract_flat: bool = False,
    playlistend: int | None = None,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
) -> dict:
    """YouTube: with any cookies, stick to *web* clients (``mweb``/android need PO tokens; browser DB skips android/ios)."""
    has_cookies = bool(cookies_file or cookies_from_browser)
    if has_cookies:
        # Netscape file (e.g. Playwright) or --cookies-from-browser: only web clients use those cookies reliably.
        player_clients = ["web", "web_embedded"]
    else:
        player_clients = ["web_embedded", "web", "mweb"]

    opts: dict = {
        "quiet": quiet,
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            },
        },
        "socket_timeout": 60,
        "retries": 6,
        "fragment_retries": 10,
        "js_runtimes": _js_runtimes(),
    }
    if os.environ.get("YT_DLP_EJS_NPM", "").strip().lower() in ("1", "true", "yes"):
        opts["remote_components"] = ["ejs:npm"]

    if extract_flat:
        opts["extract_flat"] = True
    if playlistend is not None:
        opts["playlistend"] = playlistend
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if proxy:
        opts["proxy"] = proxy
    return opts


def list_videos(
    channel_url: str,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
) -> list[dict]:
    """List all non-short videos from a YouTube channel."""
    ydl_opts = _youtube_ydl_opts(
        quiet=True,
        extract_flat=True,
        playlistend=500,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
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


def download_audio(video_id: str, output_dir: Path, cookies_file: str | None = None,
                    cookies_from_browser: str | None = None, proxy: str | None = None) -> Path | None:
    """Download audio-only from YouTube video."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{video_id}.mp3"
    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"  ⏭️  Audio exists: {output_path}")
        return output_path

    url = f"https://www.youtube.com/watch?v={video_id}"
    base = _youtube_ydl_opts(
        quiet=True,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
    base["noplaylist"] = True
    format_tries = [
        "bestaudio/bestaudio/best/worst",
        "ba/b/w",
        "bv*+ba/bestvideo+bestaudio/b",
        "best/worst",
    ]
    last_err: str | None = None
    for fmt in format_tries:
        ydl_opts = {
            **base,
            "format": fmt,
            "outtmpl": str(output_dir / f"{video_id}.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "32"}
            ],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if output_path.exists():
                return output_path
            for f in output_dir.glob(f"{video_id}.*"):
                if f.suffix == ".mp3":
                    return f
        except Exception as e:
            last_err = str(e)
            print(f"  ⚠️  format {fmt!r}: {e}")
    if last_err:
        print(f"  ❌ Download failed after {len(format_tries)} format attempts")
        print(
            "   Fix: install Node.js or Deno for n-challenge; export cookies with:\n"
            "   python tools/youtube_refresh_cookies.py\n"
            "   Optional: set YT_DLP_EJS_NPM=1 to allow yt-dlp to fetch ejs:npm components."
        )
    return None


def get_video_metadata(
    video_id: str,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
) -> dict:
    """Get full metadata for a single video."""
    ydl_opts = _youtube_ydl_opts(
        quiet=True,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
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


def _normalize_channel_url(url: str) -> str:
    u = url.strip().rstrip("/")
    u = u.replace("http://", "https://")
    return u.lower()


def create_source(name: str, channel_url: str, blogger_id: str = "yuri") -> str:
    r = requests.post("http://localhost:8002/api/v1/sources/", json={
        "name": name, "source_type": "youtube", "blogger_id": blogger_id,
        "config": {"channel_url": channel_url},
    })
    if r.status_code in (200, 201):
        sid = r.json()["id"]
        print(f"✅ Source created: {sid}")
        return sid
    try:
        detail = r.json()
    except Exception:
        detail = r.text
    print(f"⚠️  Source creation HTTP {r.status_code}: {detail}")
    if r.status_code >= 500:
        print(
            "   Hint: after restoring PostgreSQL (e.g. Drive sync), restart ingestion-service:\n"
            "   docker compose -f docker-compose.dev.yml restart ingestion-service"
        )
    return ""


def ensure_youtube_source(name: str, channel_url: str, blogger_id: str = "yuri") -> str:
    """Reuse the best matching active source (most items wins); otherwise create via API."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from common.config import DatabaseSettings

    want = _normalize_channel_url(channel_url)
    engine = sa.create_engine(DatabaseSettings().sync_url)
    with Session(engine) as session:
        rows = session.execute(
            sa.text("""
            SELECT cs.id::text AS sid,
                   (SELECT COUNT(*) FROM content_items ci WHERE ci.source_id = cs.id) AS item_count,
                   cs.config->>'channel_url' AS channel_url
            FROM content_sources cs
            WHERE cs.name = :name
              AND cs.source_type = CAST('youtube' AS sourcetype)
              AND cs.blogger_id = CAST(:bid AS bloggerid)
              AND cs.is_active IS TRUE
            """),
            {"name": name, "bid": blogger_id},
        ).mappings().all()
    candidates = [
        r for r in rows if r["channel_url"] and _normalize_channel_url(r["channel_url"]) == want
    ]
    if candidates:
        best = max(candidates, key=lambda r: (int(r["item_count"] or 0), r["sid"]))
        print(f"✅ Using existing YouTube source: {best['sid']} ({best['item_count']} items)")
        return str(best["sid"])
    return create_source(name, channel_url, blogger_id)


def upsert_item(source_id: str, video: dict, transcript: str, metadata: dict, blogger_id: str = "yuri") -> str:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from common.config import DatabaseSettings

    engine = sa.create_engine(DatabaseSettings().sync_url)
    item_id = str(uuid.uuid4())
    with Session(engine) as session:
        existing = session.execute(
            sa.text("""
                SELECT id FROM content_items
                WHERE source_id = CAST(:sid AS uuid)
                  AND (
                      title = :title
                      OR (raw_metadata->>'youtube_id') = :ytid
                  )
                LIMIT 1
            """),
            {"sid": source_id, "title": video["title"], "ytid": video["id"]},
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
    parser.add_argument("--cookies", default=None, help="Path to cookies.txt (Netscape format)")
    parser.add_argument("--cookies-from-browser", default=None, help="Browser name (e.g. 'chrome')")
    parser.add_argument(
        "--force-browser-cookies",
        action="store_true",
        help="Do not replace --cookies-from-browser with cookies/youtube.txt",
    )
    parser.add_argument("--proxy", default=None, help="Proxy URL (e.g. http://user:pass@host:port)")
    args = parser.parse_args()

    default_cookies = _repo_root() / "cookies" / "youtube.txt"
    if not args.cookies and not args.cookies_from_browser and default_cookies.is_file():
        args.cookies = str(default_cookies)
        print(f"🍪 Using default cookie file: {default_cookies}\n")
    if (
        not args.force_browser_cookies
        and default_cookies.is_file()
        and default_cookies.stat().st_size > 200
        and args.cookies_from_browser
    ):
        args.cookies = str(default_cookies)
        args.cookies_from_browser = None
        print(
            f"🍪 Preferring {default_cookies} over --cookies-from-browser "
            "(Playwright export works better with yt-dlp web client). "
            "Use --force-browser-cookies to keep the browser.\n"
        )

    _hydrate_openai_key_from_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        print("❌ OPENAI_API_KEY not set")
        sys.exit(1)

    print(f"\n📺 Scanning: {args.channel}")
    videos = list_videos(
        args.channel,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        proxy=args.proxy,
    )
    print(f"📄 Found {len(videos)} videos (≥{MIN_DURATION}s)\n")

    if not args.dry_run and not args.cookies and not args.cookies_from_browser:
        print(
            "⚠️  No cookies: YouTube often returns HTTP 403 or captcha. Run:\n"
            "   python tools/youtube_refresh_cookies.py\n"
            "   or: --cookies-from-browser firefox\n"
        )

    if args.dry_run:
        for v in videos:
            m = v["duration"] // 60
            print(f"  {v['id']} | {v['title'][:60]} | {m}min")
        print(f"\n  Total: {len(videos)} videos, {sum(v['duration'] for v in videos)//3600}h")
        return

    os.environ.setdefault("POSTGRES_HOST", "localhost")
    source_id = ensure_youtube_source(args.source_name, args.channel, args.blogger_id)
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
                print("  📄 Using transcript backup")
                transcript = transcript_file.read_text(encoding="utf-8")
            else:
                audio = download_audio(video["id"], AUDIO_DIR, args.cookies, args.cookies_from_browser, args.proxy)
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
            meta = get_video_metadata(
                video["id"],
                cookies_file=args.cookies,
                cookies_from_browser=args.cookies_from_browser,
                proxy=args.proxy,
            )
            item_id = upsert_item(source_id, video, transcript, meta, args.blogger_id)
            print(f"  💾 Saved: {item_id}")
            processed += 1

        except Exception as e:
            print(f"  ❌ Error: {e}")
            if "password authentication failed" in str(e).lower():
                print(
                    "   Hint: host scripts use POSTGRES_* from repo .env (same as Docker). "
                    "Check .env POSTGRES_PASSWORD matches docker-compose, then retry."
                )
            if "insufficient_quota" in str(e) or "billing" in str(e).lower():
                print(f"\n💰 OpenAI quota exhausted after {processed} files. Re-run with --skip-existing")
                sys.exit(2)

    print(f"\n✅ Done! {processed}/{len(videos)} videos from {args.source_name}")
    ok, qmsg = queue_transcribed_for_labeling(limit=1000)
    if ok:
        print(f"   📬 Pipeline: {qmsg}")
    else:
        print(
            f"   ⚠️  Could not reach ingestion API ({qmsg}). Queue labeling manually:\n"
            f"   curl -s -X POST '{ingestion_api_base_url()}/api/v1/jobs/queue-transcribed?limit=1000'"
        )


if __name__ == "__main__":
    main()
