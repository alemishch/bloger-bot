"""
Collect Instagram profile post/reel metadata via the **logged-in web app** (Playwright).

Uses the same persistent Chromium profile as ``instagram_refresh_cookies.py``
(``cookies/playwright_instagram``). Traffic matches a normal browser session instead of
Instaloader's private/mobile API (which often returns HTTP 429).

Requires:
    pip install playwright && playwright install chromium
    python tools/instagram_refresh_cookies.py   # log in once
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest_common import repo_root

DEFAULT_PROFILE_DIR = repo_root() / "cookies" / "playwright_instagram"


def _extract_caption(n: dict[str, Any]) -> str:
    cap = n.get("caption")
    if isinstance(cap, str):
        return cap
    if isinstance(cap, dict):
        t = cap.get("text")
        if isinstance(t, str):
            return t
    em = n.get("edge_media_to_caption")
    if isinstance(em, dict):
        edges = em.get("edges") or []
        if edges and isinstance(edges[0], dict):
            node = edges[0].get("node") or {}
            if isinstance(node.get("text"), str):
                return node["text"]
    return ""


def _extract_ts_iso(n: dict[str, Any]) -> str:
    for k in ("taken_at_timestamp", "taken_at", "device_timestamp"):
        if k in n and n[k] is not None:
            try:
                t = int(n[k])
                return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                pass
    return datetime.now(tz=timezone.utc).isoformat()


def _likes(n: dict[str, Any]) -> int:
    for key in ("edge_media_preview_like", "edge_liked_by"):
        e = n.get(key)
        if isinstance(e, dict) and e.get("count") is not None:
            try:
                return int(e["count"])
            except (TypeError, ValueError):
                pass
    if n.get("like_count") is not None:
        try:
            return int(n["like_count"])
        except (TypeError, ValueError):
            pass
    return 0


def _comments(n: dict[str, Any]) -> int:
    for key in ("edge_media_to_parent_comment", "edge_media_to_comment", "edge_threaded_comments"):
        e = n.get(key)
        if isinstance(e, dict) and e.get("count") is not None:
            try:
                return int(e["count"])
            except (TypeError, ValueError):
                pass
    if n.get("comment_count") is not None:
        try:
            return int(n["comment_count"])
        except (TypeError, ValueError):
            pass
    return 0


def _merge_media(bucket: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    code = node.get("shortcode") or node.get("code")
    if not isinstance(code, str) or len(code) < 5:
        return
    if node.get("__typename") == "GraphUser":
        return
    if node.get("edge_owner_to_timeline_media") is not None and node.get("media_type") is None:
        return
    markers = ("media_type", "is_video", "product_type", "__typename", "taken_at_timestamp", "taken_at")
    if sum(1 for m in markers if m in node) < 1:
        return

    prev = bucket.get(code)
    if prev is None:
        bucket[code] = dict(node)
        return
    merged = {**prev, **node}
    if len(_extract_caption(node)) > len(_extract_caption(prev)):
        for k in ("caption", "edge_media_to_caption"):
            if k in node:
                merged[k] = node[k]
    try:
        if float(node.get("video_duration") or 0) > float(prev.get("video_duration") or 0):
            merged["video_duration"] = node.get("video_duration")
    except (TypeError, ValueError):
        pass
    bucket[code] = merged


def _walk_json(obj: Any, bucket: dict[str, dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        _merge_media(bucket, obj)
        for v in obj.values():
            _walk_json(v, bucket)
    elif isinstance(obj, list):
        for x in obj:
            _walk_json(x, bucket)


def _is_video_node(n: dict[str, Any]) -> bool:
    if n.get("is_video") is True:
        return True
    mt = n.get("media_type")
    if mt == 2:
        return True
    if isinstance(mt, str) and mt.upper() == "VIDEO":
        return True
    return False


def _is_reel_node(n: dict[str, Any]) -> bool:
    pt = str(n.get("product_type") or "").lower()
    if pt == "clips":
        return True
    typename = str(n.get("__typename") or "")
    if typename == "GraphVideo":
        dur = float(n.get("video_duration") or 0)
        return dur > 5
    if _is_video_node(n):
        dur = float(n.get("video_duration") or 0)
        return dur > 5
    return False


def _node_to_item(code: str, n: dict[str, Any]) -> dict[str, Any]:
    cap = _extract_caption(n)
    dur = float(n.get("video_duration") or 0)
    reel = _is_reel_node(n)
    url = (
        f"https://www.instagram.com/reel/{code}/"
        if reel
        else f"https://www.instagram.com/p/{code}/"
    )
    return {
        "shortcode": code,
        "url": url,
        "timestamp": _extract_ts_iso(n),
        "likes": _likes(n),
        "comments": _comments(n),
        "caption": cap,
        "is_video": _is_video_node(n),
        "video_duration": dur if dur else None,
        "typename": str(n.get("__typename") or ""),
    }


def list_reels_and_posts_via_playwright(
    profile_username: str,
    *,
    user_data_dir: Path | None = None,
    headless: bool = False,
    scroll_rounds: int = 28,
    pause_ms: int = 2200,
    reels_extra_rounds: int = 20,
) -> tuple[list[dict], list[dict]]:
    """
    Open the profile (grid + /reels/), scroll slowly, parse all JSON XHR/fetch bodies
    that contain media nodes. Returns (reels, text_posts) in the same shape as Instaloader path.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Install Playwright:\n  pip install playwright\n  playwright install chromium",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    ud = Path(user_data_dir or DEFAULT_PROFILE_DIR)
    if not ud.is_dir():
        print(
            f"No Playwright profile at {ud}\n"
            "Run: python tools/instagram_refresh_cookies.py\n"
            "Log in, then retry.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    bucket: dict[str, dict[str, Any]] = {}

    def on_response(response) -> None:
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if "instagram.com" not in response.url:
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type") or ""
            if "application/json" not in ct and "text/javascript" not in ct:
                return
            data = response.json()
        except Exception:
            return
        _walk_json(data, bucket)

    print(f"  🌐 Playwright listing (profile dir: {ud})")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(ud),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)

        grid_url = f"https://www.instagram.com/{profile_username}/"
        page.goto(grid_url, wait_until="domcontentloaded", timeout=120_000)
        time.sleep(min(pause_ms / 1000.0, 4.0))
        if "/accounts/login" in page.url:
            context.close()
            print(
                "❌ Browser opened the login page — session expired or profile empty.\n"
                "Run: python tools/instagram_refresh_cookies.py (log in), then retry.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        for i in range(scroll_rounds):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(pause_ms)
            if i and i % 6 == 0:
                print(f"    … grid scroll {i}/{scroll_rounds} ({len(bucket)} media nodes seen in JSON)")

        reels_url = f"https://www.instagram.com/{profile_username}/reels/"
        page.goto(reels_url, wait_until="domcontentloaded", timeout=120_000)
        time.sleep(min(pause_ms / 1000.0, 3.0))
        for i in range(reels_extra_rounds):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(pause_ms)

        context.close()

    if not bucket:
        print(
            "⚠️  No media JSON captured. Instagram may have changed their web API, or the grid did not load.\n"
            "Try: non-headless window, slower scrolling (--ig-scroll-pause-ms 3500), or --list-via instaloader.",
            file=sys.stderr,
        )

    reels: list[dict] = []
    posts: list[dict] = []
    reel_codes: set[str] = set()

    for code, raw in bucket.items():
        item = _node_to_item(code, raw)
        if _is_reel_node(raw):
            reels.append(item)
            reel_codes.add(code)

    for code, raw in bucket.items():
        if code in reel_codes:
            continue
        if _is_video_node(raw):
            continue
        cap = _extract_caption(raw)
        if len(cap) < 100:
            continue
        posts.append(_node_to_item(code, raw))

    def _sort_key(x: dict) -> str:
        return x.get("timestamp") or ""

    reels.sort(key=_sort_key, reverse=True)
    posts.sort(key=_sort_key, reverse=True)
    return reels, posts
