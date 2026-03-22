"""
Export Instagram cookies (Netscape) for instaloader + yt-dlp, using Playwright Chromium.

Requires ``sessionid`` and ``csrftoken`` in the jar for instaloader's ``load_session``.

    pip install playwright
    playwright install chromium
    python tools/instagram_refresh_cookies.py

Log in when the window opens, open instagram.com home, press Enter → writes ``cookies/instagram.txt``.
The same Chromium profile is used by ``ingest_instagram.py`` (default ``--list-via playwright``) to list
posts/reels without Instaloader's mobile API.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cookie_list_to_netscape(cookies: list[dict]) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# tools/instagram_refresh_cookies.py",
        "",
    ]
    for c in cookies:
        domain = c.get("domain") or ""
        include = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = int(c.get("expires") or 0)
        if exp < 0:
            exp = 0
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\t{include}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Install Playwright:\n  pip install playwright\n  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    root = _repo_root()
    cookies_dir = root / "cookies"
    profile = cookies_dir / "playwright_instagram"
    out = cookies_dir / "instagram.txt"
    cookies_dir.mkdir(parents=True, exist_ok=True)

    print("Launching Chromium (persistent profile)…")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=120_000)
        input(
            "Log in to Instagram in this window if needed.\n"
            "When the feed loads, press Enter here to save cookies…\n"
        )
        raw = context.cookies()
        context.close()

    keep = [c for c in raw if "instagram" in (c.get("domain") or "").lower()]
    if len(keep) < 3:
        print(f"Warning: only {len(keep)} instagram cookies.", file=sys.stderr)

    names = {c.get("name") for c in keep}
    if "sessionid" not in names:
        print("Warning: no sessionid cookie — instaloader will not stay logged in.", file=sys.stderr)
    if "csrftoken" not in names:
        print("Warning: no csrftoken — instaloader may fail; reload instagram.com and retry.", file=sys.stderr)

    out.write_text(cookie_list_to_netscape(keep), encoding="utf-8")
    print(f"Wrote {len(keep)} cookies to {out}")
    print("Run: python tools/ingest_instagram.py --username YOUR_PROFILE_TO_SCRAPE ...")


if __name__ == "__main__":
    main()
