"""
Export YouTube cookies for yt-dlp using a real Chromium profile (Playwright).

Firefox + ``--cookies-from-browser`` often leaves yt-dlp on ``mweb``/PO-token paths.
A Netscape cookie file + ``web`` / ``web_embedded`` clients is more reliable.

Setup (once per machine):

    pip install playwright
    playwright install chromium
    python tools/youtube_refresh_cookies.py

A window opens: sign in to Google if asked, wait until YouTube loads, press Enter.
Cookies are written to ``cookies/youtube.txt`` (gitignored). Re-run when downloads fail.

Then:

    python tools/ingest_youtube.py --channel "..." --skip-existing
    # or: make ingest-youtube CHANNEL="..."
"""
from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cookie_list_to_netscape(cookies: list[dict]) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# tools/youtube_refresh_cookies.py — for yt-dlp",
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
            "Install Playwright first:\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    root = _repo_root()
    cookies_dir = root / "cookies"
    profile = cookies_dir / "playwright_youtube"
    out = cookies_dir / "youtube.txt"
    cookies_dir.mkdir(parents=True, exist_ok=True)

    print("Launching Chromium (persistent profile)…")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=120_000)
        input(
            "Sign in to Google if YouTube asks.\n"
            "When the site works in this window, press Enter here to save cookies…\n"
        )
        raw = context.cookies()
        context.close()

    keep: list[dict] = []
    for c in raw:
        d = (c.get("domain") or "").lower()
        if "youtube" in d or d.endswith(".google.com") or d == "google.com":
            keep.append(c)

    if len(keep) < 3:
        print(f"Warning: only {len(keep)} youtube/google cookies — login may have failed.", file=sys.stderr)

    out.write_text(cookie_list_to_netscape(keep), encoding="utf-8")
    print(f"Wrote {len(keep)} cookies to {out}")
    print("Run ingest without --cookies-from-browser; ingest prefers cookies/youtube.txt.")


if __name__ == "__main__":
    main()
