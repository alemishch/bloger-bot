"""Shared helpers for local ingest scripts (YouTube, Instagram, …)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def hydrate_openai_key_from_dotenv() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    path = repo_root() / ".env"
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


def js_runtimes_dict() -> dict:
    runtimes: dict = {}
    for exe in ("node", "deno", "bun"):
        if shutil.which(exe):
            runtimes[exe] = {}
    if not runtimes:
        runtimes["deno"] = {}
    return runtimes


def netscape_cookie_dict(path: Path, domain_must_contain: str) -> dict[str, str]:
    """Parse Netscape cookies.txt; keep lines whose domain contains *domain_must_contain* (case-insensitive)."""
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name, value = parts[0], parts[5], parts[6]
        if domain_must_contain.lower() not in domain.lower():
            continue
        out[name] = value
    return out


def yt_dlp_network_opts(
    *,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    quiet: bool = True,
) -> dict:
    opts: dict = {
        "quiet": quiet,
        "socket_timeout": 60,
        "retries": 6,
        "fragment_retries": 10,
        "js_runtimes": js_runtimes_dict(),
    }
    if os.environ.get("YT_DLP_EJS_NPM", "").strip().lower() in ("1", "true", "yes"):
        opts["remote_components"] = ["ejs:npm"]
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if proxy:
        opts["proxy"] = proxy
    return opts


def ingestion_api_base_url() -> str:
    """Base URL for ingestion-service (no trailing slash)."""
    return os.environ.get("INGESTION_API_URL", "http://localhost:8002").rstrip("/")


def queue_transcribed_for_labeling(limit: int = 1000) -> tuple[bool, str]:
    """
    Tell ingestion-service to enqueue Celery ``label_item`` tasks for rows in ``transcribed``.
    Local ingest scripts insert directly as transcribed; without this call, nothing moves to labeling.
    """
    import requests

    url = f"{ingestion_api_base_url()}/api/v1/jobs/queue-transcribed"
    batch = min(max(limit, 1), 1000)
    try:
        r = requests.post(url, params={"limit": batch}, timeout=120)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:400]}"
        data = r.json()
        n = int(data.get("queued", 0))
        return True, f"queued {n} item(s) for labeling"
    except Exception as e:
        return False, str(e)
