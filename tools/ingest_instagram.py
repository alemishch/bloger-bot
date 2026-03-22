"""
Ingest content from an Instagram profile (separate sources for posts vs reels).

- **Posts** (text): captions from the main grid (instaloader ``get_posts``, non-video).
- **Reels**: ``Profile.get_reels()`` when logged in, then yt-dlp downloads audio → Whisper.

Cookie flow (same idea as YouTube):

1. ``pip install playwright && playwright install chromium``
2. ``python tools/instagram_refresh_cookies.py`` → ``cookies/instagram.txt``
3. ``python tools/ingest_instagram.py --username profile_to_scrape``

``--ig-login-username`` is the **logged-in** account in the cookie jar (defaults to ``--username`` if you
scrape your own profile). For a different target profile, set ``--username target`` and
``--ig-login-username your_login``.

**Listing (default):** Playwright + your saved Chromium session (``cookies/playwright_instagram``) — same
session as ``instagram_refresh_cookies.py``. Collects JSON from the web app (avoids Instaloader's
mobile API, which often returns HTTP 429).

**Listing (fallback):** ``--list-via instaloader`` — private API; may rate-limit.

Reel audio: yt-dlp + Netscape cookies (like ``ingest_youtube``).

Usage:
    python tools/ingest_instagram.py --username kinashyuriy --dry-run
    python tools/ingest_instagram.py --username kinashyuriy --skip-existing
    python tools/ingest_instagram.py --username kinashyuriy --reels-only
    python tools/ingest_instagram.py --username kinashyuriy --posts-only
    python tools/ingest_instagram.py --username kinashyuriy --list-via instaloader
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))

import requests
import yt_dlp

from ingest_common import (
    hydrate_openai_key_from_dotenv,
    ingestion_api_base_url,
    netscape_cookie_dict,
    queue_transcribed_for_labeling,
    repo_root,
    yt_dlp_network_opts,
)
from instagram_playwright_list import DEFAULT_PROFILE_DIR, list_reels_and_posts_via_playwright

AUDIO_DIR = Path("data/instagram_audio")
TRANSCRIPTS_DIR = Path("data/instagram_transcripts")
WHISPER_MAX_BYTES = 24 * 1024 * 1024
MIN_CAPTION_LENGTH = 100


def _safe_name_part(s: str) -> str:
    t = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.strip())
    return (t[:80].strip("_") or "user").lower()


def _build_instaloader(
    login_username: str,
    ig_session: str | None,
    cookies_path: Path | None,
):
    import instaloader

    L = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    session_path = Path(ig_session).expanduser() if ig_session else None
    if session_path and session_path.is_file():
        L.load_session_from_file(login_username, str(session_path))
        print(f"  🔑 Instaloader session file: {session_path}")
    elif cookies_path and cookies_path.is_file():
        jar = netscape_cookie_dict(cookies_path, "instagram")
        if not jar.get("sessionid"):
            print("  ⚠️  cookies/instagram.txt has no sessionid — run tools/instagram_refresh_cookies.py")
        else:
            L.load_session(login_username, jar)
            print(f"  🔑 Instaloader session from Netscape cookies ({len(jar)} keys)")
    else:
        print("  ⚠️  No session/cookies: public data only; reels listing may fail.")
    return L


def list_reels_and_posts(
    profile_username: str,
    login_username: str,
    ig_session: str | None,
    cookies_path: Path | None,
) -> tuple[list[dict], list[dict]]:
    import instaloader

    L = _build_instaloader(login_username, ig_session, cookies_path)
    profile = instaloader.Profile.from_username(L.context, profile_username)

    reels: list[dict] = []
    try:
        for post in profile.get_reels():
            try:
                dur = getattr(post, "video_duration", None) or 0
            except Exception:
                dur = 0
            reels.append(
                {
                    "shortcode": post.shortcode,
                    "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                    "timestamp": post.date_utc.isoformat(),
                    "likes": post.likes,
                    "comments": post.comments,
                    "caption": post.caption or "",
                    "is_video": True,
                    "video_duration": dur,
                    "typename": post.typename,
                }
            )
    except Exception as e:
        print(f"  ⚠️  get_reels() failed ({e}); falling back to scanning get_posts() for videos.")
        reel_codes: set[str] = set()
        for post in profile.get_posts():
            if not post.is_video:
                continue
            if post.typename != "GraphVideo":
                continue
            dur = getattr(post, "video_duration", None) or 0
            if dur <= 5:
                continue
            meta = {
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                "timestamp": post.date_utc.isoformat(),
                "likes": post.likes,
                "comments": post.comments,
                "caption": post.caption or "",
                "is_video": True,
                "video_duration": dur,
                "typename": post.typename,
            }
            reels.append(meta)
            reel_codes.add(post.shortcode)
    else:
        reel_codes = {r["shortcode"] for r in reels}

    posts: list[dict] = []
    for post in profile.get_posts():
        if post.shortcode in reel_codes:
            continue
        cap = post.caption or ""
        if post.is_video:
            continue
        if len(cap) < MIN_CAPTION_LENGTH:
            continue
        posts.append(
            {
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "timestamp": post.date_utc.isoformat(),
                "likes": post.likes,
                "comments": post.comments,
                "caption": cap,
                "is_video": False,
                "video_duration": None,
                "typename": post.typename,
            }
        )

    return reels, posts


def download_reel_audio(
    shortcode: str,
    output_dir: Path,
    *,
    cookies_file: str | None,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{shortcode}.mp3"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    url = f"https://www.instagram.com/reel/{shortcode}/"
    base = yt_dlp_network_opts(
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
        quiet=True,
    )
    base["noplaylist"] = True
    format_tries = [
        "bestaudio/bestaudio/best/worst",
        "ba/b/w",
        "bv*+ba/best/worst",
        "best/worst",
    ]
    last_err = None
    for fmt in format_tries:
        ydl_opts = {
            **base,
            "format": fmt,
            "outtmpl": str(output_dir / f"{shortcode}.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "32"}
            ],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if output_path.exists():
                return output_path
            for f in output_dir.glob(f"{shortcode}.*"):
                if f.suffix == ".mp3":
                    return f
        except Exception as e:
            last_err = str(e)
            print(f"  ⚠️  yt-dlp format {fmt!r}: {e}")
    if last_err:
        print("  ❌ Reel download failed. Try: python tools/instagram_refresh_cookies.py")
    return None


def split_audio(audio_path: Path) -> list[Path]:
    import math

    size = audio_path.stat().st_size
    if size <= WHISPER_MAX_BYTES:
        return [audio_path]
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
        capture_output=True,
        text=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    n = math.ceil(size / WHISPER_MAX_BYTES)
    seg = int(duration / n) + 1
    pattern = str(audio_path.with_suffix("")) + "_part%03d.mp3"
    subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-f", "segment", "-segment_time", str(seg), "-c", "copy", pattern, "-y"],
        capture_output=True,
    )
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


def create_source(name: str, source_type: str, instagram_username: str, blogger_id: str = "yuri") -> str:
    r = requests.post(
        "http://localhost:8002/api/v1/sources/",
        json={
            "name": name,
            "source_type": source_type,
            "blogger_id": blogger_id,
            "config": {
                "instagram_username": instagram_username,
                "instagram_url": f"https://www.instagram.com/{instagram_username}/",
            },
        },
    )
    if r.status_code in (200, 201):
        sid = r.json()["id"]
        print(f"✅ Source '{name}' created: {sid}")
        return sid
    print(f"⚠️  Source HTTP {r.status_code}: {r.text[:800]}")
    return ""


def ensure_instagram_source(
    name: str,
    source_type: str,
    instagram_username: str,
    blogger_id: str = "yuri",
) -> str:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from common.config import DatabaseSettings

    engine = sa.create_engine(DatabaseSettings().sync_url)
    with Session(engine) as session:
        rows = session.execute(
            sa.text("""
                SELECT cs.id::text AS sid,
                       (SELECT COUNT(*) FROM content_items ci WHERE ci.source_id = cs.id) AS item_count
                FROM content_sources cs
                WHERE cs.name = :name
                  AND cs.source_type = CAST(:stype AS sourcetype)
                  AND cs.blogger_id = CAST(:bid AS bloggerid)
                  AND cs.is_active IS TRUE
                  AND cs.config->>'instagram_username' = :iu
            """),
            {
                "name": name,
                "stype": source_type,
                "bid": blogger_id,
                "iu": instagram_username,
            },
        ).mappings().all()
    if rows:
        best = max(rows, key=lambda r: (int(r["item_count"] or 0), r["sid"]))
        print(f"✅ Using existing source '{name}': {best['sid']} ({best['item_count']} items)")
        return str(best["sid"])
    return create_source(name, source_type, instagram_username, blogger_id)


def upsert_item(
    source_id: str,
    content_type: str,
    title: str,
    text: str,
    meta: dict,
    blogger_id: str = "yuri",
) -> str:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from common.config import DatabaseSettings

    engine = sa.create_engine(DatabaseSettings().sync_url)
    item_id = str(uuid.uuid4())
    sc = meta.get("shortcode", "")
    with Session(engine) as session:
        existing = session.execute(
            sa.text("""
                SELECT id FROM content_items
                WHERE source_id = CAST(:sid AS uuid)
                  AND (
                      title = :title
                      OR (raw_metadata->>'shortcode') = :sc
                  )
                LIMIT 1
            """),
            {"sid": source_id, "title": title, "sc": sc},
        ).first()
        if existing:
            print(f"  ⏭️  Exists: {title}")
            return str(existing[0])

        ct = "video" if content_type == "reel" else "post"
        session.execute(
            sa.text("""
                INSERT INTO content_items
                (id, source_id, content_type, blogger_id, status, title, transcript_text, text,
                 source_url, duration_seconds, raw_metadata, created_at, updated_at, retry_count)
                VALUES (CAST(:id AS uuid), CAST(:sid AS uuid),
                        CAST(:ct AS contenttype), CAST(:bid AS bloggerid),
                        CAST('transcribed' AS jobstatus),
                        :title, :transcript, :text, :url, :dur,
                        CAST(:meta AS json), NOW(), NOW(), 0)
            """),
            {
                "id": item_id,
                "sid": source_id,
                "bid": blogger_id,
                "ct": ct,
                "title": title,
                "transcript": text,
                "text": text,
                "url": meta.get("url", ""),
                "dur": meta.get("video_duration"),
                "meta": json.dumps(meta, ensure_ascii=False, default=str),
            },
        )
        session.commit()
    return item_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Instagram reels + post text")
    parser.add_argument("--username", required=True, help="Profile to scrape (public or visible to login)")
    parser.add_argument(
        "--ig-login-username",
        default=None,
        help="Instagram account that owns the cookies/session (default: same as --username)",
    )
    parser.add_argument("--blogger-id", default="yuri")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--reels-only", action="store_true")
    parser.add_argument("--posts-only", action="store_true")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--cookies", default=None, help="Netscape cookies for yt-dlp (reels); default cookies/instagram.txt")
    parser.add_argument("--cookies-from-browser", default=None, help="e.g. chrome — inferior to Playwright export")
    parser.add_argument("--force-browser-cookies", action="store_true")
    parser.add_argument("--ig-session", default=None, help="Instaloader pickle session file (overrides Netscape)")
    parser.add_argument(
        "--list-via",
        choices=("playwright", "instaloader"),
        default="playwright",
        help="How to discover posts/reels (default: playwright = web session, fewer 429s)",
    )
    parser.add_argument(
        "--playwright-profile",
        default=None,
        help="Chromium user_data_dir (default: cookies/playwright_instagram)",
    )
    parser.add_argument(
        "--playwright-headless",
        action="store_true",
        help="Run browser headless for listing (less reliable if IG challenges you)",
    )
    parser.add_argument("--ig-scroll-rounds", type=int, default=28, help="Grid scroll iterations (playwright)")
    parser.add_argument(
        "--ig-reels-scroll-rounds",
        type=int,
        default=20,
        help="Extra scroll iterations on /reels/ (playwright)",
    )
    parser.add_argument(
        "--ig-scroll-pause-ms",
        type=int,
        default=2200,
        help="Pause between scroll steps in ms (playwright)",
    )
    parser.add_argument("--proxy", default=None)
    args = parser.parse_args()

    login_u = args.ig_login_username or args.username
    profile_u = args.username

    default_cookies = repo_root() / "cookies" / "instagram.txt"
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
            "(use --force-browser-cookies to keep browser).\n"
        )

    cookies_path = Path(args.cookies).expanduser() if args.cookies else None

    hydrate_openai_key_from_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run and not args.posts_only:
        print("❌ OPENAI_API_KEY needed for reel transcription")
        sys.exit(1)

    tag = _safe_name_part(profile_u)
    post_source_name = f"ig_{tag}_posts"
    reel_source_name = f"ig_{tag}_reels"

    print(f"\n📸 Instagram profile: @{profile_u} (login session: @{login_u})")
    print("  Fetching posts / reels…")
    if args.list_via == "playwright":
        pw_profile = Path(args.playwright_profile).expanduser() if args.playwright_profile else DEFAULT_PROFILE_DIR
        reels, posts = list_reels_and_posts_via_playwright(
            profile_u,
            user_data_dir=pw_profile,
            headless=args.playwright_headless,
            scroll_rounds=args.ig_scroll_rounds,
            pause_ms=args.ig_scroll_pause_ms,
            reels_extra_rounds=args.ig_reels_scroll_rounds,
        )
    else:
        reels, posts = list_reels_and_posts(profile_u, login_u, args.ig_session, cookies_path)
    print(f"  🎬 Reels: {len(reels)}")
    print(f"  📝 Text posts (≥{MIN_CAPTION_LENGTH} chars): {len(posts)}\n")

    if args.list_via == "instaloader" and not args.cookies and not args.cookies_from_browser and not args.ig_session:
        print(
            "⚠️  No cookies/session: Instaloader often hits HTTP 429. Run:\n"
            "   python tools/instagram_refresh_cookies.py\n"
        )
    elif (
        args.list_via == "playwright"
        and not args.posts_only
        and not args.cookies
        and not args.cookies_from_browser
    ):
        print(
            "⚠️  No cookies/instagram.txt for yt-dlp — reel audio download may fail. Run:\n"
            "   python tools/instagram_refresh_cookies.py\n"
        )

    if args.dry_run:
        if not args.posts_only:
            print("=== REELS (sample) ===")
            for r in reels[:15]:
                print(f"  {r['shortcode']} | {r['caption'][:50]!r}… | {r.get('video_duration', 0)}s")
            if len(reels) > 15:
                print(f"  … +{len(reels) - 15} more")
        if not args.reels_only:
            print("\n=== POSTS (sample) ===")
            for p in posts[:15]:
                print(f"  {p['shortcode']} | {p['caption'][:60]!r}…")
            if len(posts) > 15:
                print(f"  … +{len(posts) - 15} more")
        return

    os.environ.setdefault("POSTGRES_HOST", "localhost")

    if not args.reels_only and posts:
        post_source_id = ensure_instagram_source(
            post_source_name, "instagram_post", profile_u, args.blogger_id
        )
        if post_source_id:
            saved = 0
            for p in posts:
                title = f"ig_post_{p['shortcode']}"
                upsert_item(post_source_id, "post", title, p["caption"], p, args.blogger_id)
                saved += 1
            print(f"  💾 Posts upserted: {saved}\n")

    if not args.posts_only and reels:
        reel_source_id = ensure_instagram_source(
            reel_source_name, "instagram_reels", profile_u, args.blogger_id
        )
        if reel_source_id:
            processed = 0
            for i, r in enumerate(reels):
                if args.max_files and processed >= args.max_files:
                    break
                vd = r.get("video_duration")
                vd_s = f"{vd}s" if vd is not None else "?s"
                print(f"\n[{i+1}/{len(reels)}] Reel {r['shortcode']} ({vd_s})")

                transcript_file = TRANSCRIPTS_DIR / f"{r['shortcode']}.txt"
                try:
                    if transcript_file.exists() and transcript_file.stat().st_size > 50:
                        transcript = transcript_file.read_text(encoding="utf-8")
                        print("  📄 Using transcript backup")
                    else:
                        audio = download_reel_audio(
                            r["shortcode"],
                            AUDIO_DIR,
                            cookies_file=args.cookies,
                            cookies_from_browser=args.cookies_from_browser,
                            proxy=args.proxy,
                        )
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
                    print(f"  💾 Saved ({len(transcript)} chars transcript)")
                    processed += 1

                except Exception as e:
                    print(f"  ❌ {e}")
                    if "insufficient_quota" in str(e):
                        print("\n💰 Quota exhausted. Re-run with --skip-existing")
                        sys.exit(2)

            print(f"\n✅ Reels processed: {processed}/{len(reels)}")

    print("\n✅ Instagram ingestion complete.")
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
