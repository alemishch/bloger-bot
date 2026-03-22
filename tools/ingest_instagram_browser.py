"""
Legacy Selenium scraper (Linux-oriented Chrome profile paths).

Prefer instead:

- ``python tools/instagram_refresh_cookies.py`` → ``cookies/instagram.txt``
- ``python tools/ingest_instagram.py --username …`` (instaloader + yt-dlp)

This script is kept for reference only.

Usage:
    python tools/ingest_instagram_browser.py --username kinashyuriy --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs", "common", "src"))

import requests

MIN_CAPTION_LENGTH = 100


def scrape_profile(username: str, max_scroll: int = 50) -> list[dict]:
    """Scrape Instagram profile using selenium with existing Chrome session."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-data-dir={os.path.expanduser('~/.config/google-chrome')}")
    opts.add_argument("--profile-directory=Default")

    driver = webdriver.Chrome(options=opts)
    posts = []

    try:
        url = f"https://www.instagram.com/{username}/"
        print(f"  Opening {url}")
        driver.get(url)
        time.sleep(5)

        last_height = driver.execute_script("return document.body.scrollHeight")
        scroll_count = 0

        while scroll_count < max_scroll:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
            scroll_count += 1
            if scroll_count % 5 == 0:
                print(f"  Scrolled {scroll_count} times...")

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/p/'], a[href*='/reel/']")
        seen = set()
        for link in links:
            href = link.get_attribute("href") or ""
            if "/p/" in href or "/reel/" in href:
                shortcode = href.rstrip("/").split("/")[-1]
                if shortcode not in seen:
                    seen.add(shortcode)
                    is_reel = "/reel/" in href
                    posts.append({
                        "shortcode": shortcode,
                        "url": href,
                        "is_reel": is_reel,
                    })

        print(f"  Found {len(posts)} posts/reels")

        for i, post in enumerate(posts[:200]):
            try:
                driver.get(post["url"])
                time.sleep(2)

                caption_els = driver.find_elements(By.CSS_SELECTOR, "h1, span[class*='_ap3a']")
                caption = ""
                for el in caption_els:
                    text = el.text.strip()
                    if len(text) > len(caption):
                        caption = text

                meta_els = driver.find_elements(By.CSS_SELECTOR, "span[class*='html-span']")
                for el in meta_els:
                    text = el.text.strip()
                    if len(text) > len(caption) and len(text) > 50:
                        caption = text

                post["caption"] = caption

                if (i + 1) % 10 == 0:
                    print(f"  Scraped {i+1}/{min(len(posts),200)} posts")

            except Exception as e:
                post["caption"] = ""

    finally:
        driver.quit()

    return posts


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
             source_url, raw_metadata, created_at, updated_at, retry_count)
            VALUES (CAST(:id AS uuid), CAST(:sid AS uuid),
                    CAST(:ct AS contenttype), CAST(:bid AS bloggerid),
                    CAST('transcribed' AS jobstatus),
                    :title, :text, :text, :url,
                    CAST(:meta AS json), NOW(), NOW(), 0)
        """), {
            "id": item_id, "sid": source_id, "bid": blogger_id, "ct": ct,
            "title": title, "text": text, "url": meta.get("url", ""),
            "meta": json.dumps(meta, ensure_ascii=False, default=str),
        })
        session.commit()
    return item_id


def main():
    parser = argparse.ArgumentParser(description="Ingest Instagram via browser session")
    parser.add_argument("--username", required=True)
    parser.add_argument("--blogger-id", default="yuri")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--posts-only", action="store_true")
    parser.add_argument("--max-scroll", type=int, default=50, help="Max scroll iterations")
    args = parser.parse_args()

    print(f"\n📸 Scraping Instagram: @{args.username} (via browser session)")
    posts = scrape_profile(args.username, args.max_scroll)

    text_posts = [p for p in posts if not p.get("is_reel") and len(p.get("caption", "")) >= MIN_CAPTION_LENGTH]
    reels = [p for p in posts if p.get("is_reel")]

    print(f"  📝 Text posts (≥{MIN_CAPTION_LENGTH} chars): {len(text_posts)}")
    print(f"  🎬 Reels: {len(reels)}")

    if args.dry_run:
        for p in text_posts[:10]:
            print(f"  POST {p['shortcode']}: {p.get('caption','')[:60]}...")
        for r in reels[:10]:
            print(f"  REEL {r['shortcode']}: {r.get('caption','')[:60]}...")
        return

    os.environ.setdefault("POSTGRES_HOST", "localhost")

    if text_posts:
        source_id = create_source("inst_post", "instagram_post", args.username, args.blogger_id)
        if source_id:
            saved = 0
            for p in text_posts:
                title = f"ig_post_{p['shortcode']}"
                upsert_item(source_id, "post", title, p["caption"], p, args.blogger_id)
                saved += 1
            print(f"  💾 Saved {saved} posts")

    if reels and not args.posts_only:
        print(f"\n  ℹ️  Reels ({len(reels)}) found but transcription requires separate download.")
        print(f"  Reel URLs saved in metadata. Use ingest_instagram.py --reels-only when Instagram unblocks.")

    print(f"\n✅ Done!")
    print(f"   Queue labeling: curl -s -X POST 'http://localhost:8002/api/v1/jobs/queue-transcribed?limit=500'")


if __name__ == "__main__":
    main()
