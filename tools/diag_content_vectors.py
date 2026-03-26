#!/usr/bin/env python3
"""Compare ingestion pipeline stats (Postgres) vs Chroma collection counts.

Run from repo root while Docker publishes ports 8002 (ingestion) and 8000 (Chroma):

  python tools/diag_content_vectors.py
  python tools/diag_content_vectors.py --chroma-host localhost --chroma-port 8000

If ``ready`` in API is high but Chroma ``blogger_*`` count is 0, run:

  make revectorize-ready
  # and ensure: docker compose -f docker-compose.dev.yml up -d ingestion-worker
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _get_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Postgres vs Chroma content state.")
    parser.add_argument("--ingestion", default="http://localhost:8002", help="Ingestion API base URL")
    parser.add_argument("--chroma-host", default="localhost")
    parser.add_argument("--chroma-port", type=int, default=8000)
    parser.add_argument(
        "--collections",
        default="blogger_yuri,blogger_maria",
        help="Comma-separated Chroma collection names to probe",
    )
    args = parser.parse_args()

    stats_url = f"{args.ingestion.rstrip('/')}/api/v1/jobs/stats"
    try:
        stats = _get_json(stats_url)
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach ingestion at {stats_url!r}: {e}") from e

    print("=== Ingestion API (content_items by status) ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    ready = int(stats.get("ready", 0))

    try:
        import chromadb
    except ImportError:
        print("\n(install chromadb for Chroma counts: pip install chromadb)")
        return

    client = chromadb.HttpClient(host=args.chroma_host, port=args.chroma_port)
    names = [n.strip() for n in args.collections.split(",") if n.strip()]
    print("\n=== Chroma collections ===")
    for name in names:
        try:
            col = client.get_collection(name=name)
            n = col.count()
            print(f"  {name}: {n} documents")
        except Exception as e:
            print(f"  {name}: error — {e}")

    if ready > 0:
        print(
            "\nHint: if ``ready`` is large but Chroma doc count is 0, Postgres and Chroma are out of sync. "
            "Queue re-vectorization: POST /api/v1/jobs/queue-revectorize-ready or `make revectorize-ready` "
            "(with ingestion-worker running).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
