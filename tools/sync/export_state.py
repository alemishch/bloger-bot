"""
Export everything needed to resume pipeline on another machine.
Run this BEFORE unplugging USB / switching laptops.

Usage:
    python tools/sync/export_state.py --output /path/to/usb/bloger-bot-sync
"""
import os
import subprocess
import shutil
import json
import argparse
from datetime import datetime
from pathlib import Path


def run(cmd: str, **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"  ⚠️  stderr: {result.stderr.strip()}")
    return result


def export_state(output_dir: str):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n📦 Exporting bloger-bot state → {output}")
    print(f"   Timestamp: {timestamp}\n")

    # ── 1. Export PostgreSQL ──
    print("1️⃣  Dumping PostgreSQL...")
    db_dump = output / "postgres_dump.sql"
    result = run(
        f'docker compose -f docker-compose.dev.yml exec -T postgres '
        f'pg_dump -U bloger_bot bloger_bot > "{db_dump}"'
    )
    if db_dump.exists() and db_dump.stat().st_size > 0:
        print(f"   ✅ DB dump: {db_dump.stat().st_size // 1024} KB\n")
    else:
        print("   ❌ DB dump failed! Check docker is running.\n")

    # ── 2. Copy session files ──
    print("2️⃣  Copying session files...")
    sessions_dir = output / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    session_files = (
        list(Path(".").glob("*.session"))
        + list(Path("tools").glob("*.session"))
        + list(Path("sessions").glob("*.session") if Path("sessions").exists() else [])
    )
    for sf in session_files:
        dest = sessions_dir / sf.name
        shutil.copy2(sf, dest)
        print(f"   ✅ {sf} → {dest.name}")
    print()

    # ── 3. Copy transcriptions (small, critical) ──
    print("3️⃣  Copying transcriptions...")
    trans_src = Path("data/transcriptions")
    if trans_src.exists():
        trans_dst = output / "transcriptions"
        if trans_dst.exists():
            shutil.rmtree(trans_dst)
        shutil.copytree(trans_src, trans_dst)
        count = len(list(trans_dst.glob("*.json")))
        print(f"   ✅ {count} transcription files copied\n")
    else:
        print("   ⏭️  No transcriptions yet\n")

    # ── 4. Copy labeled data ──
    print("4️⃣  Copying labeled data...")
    labeled_src = Path("data/labeled")
    if labeled_src.exists():
        labeled_dst = output / "labeled"
        if labeled_dst.exists():
            shutil.rmtree(labeled_dst)
        shutil.copytree(labeled_src, labeled_dst)
        count = len(list(labeled_dst.glob("*.json")))
        print(f"   ✅ {count} labeled files copied\n")
    else:
        print("   ⏭️  No labeled data yet\n")

    # ── 5. Optionally copy downloads (large, skip by default) ──
    downloads_src = Path("data/downloads")
    if downloads_src.exists():
        total_size = sum(f.stat().st_size for f in downloads_src.rglob("*") if f.is_file())
        total_gb = total_size / (1024 ** 3)
        print(f"5️⃣  Downloads folder: {total_gb:.2f} GB")
        if total_gb < 10:  # auto-copy if under 10 GB
            downloads_dst = output / "downloads"
            if downloads_dst.exists():
                shutil.rmtree(downloads_dst)
            shutil.copytree(downloads_src, downloads_dst)
            print(f"   ✅ Copied (under 10 GB threshold)\n")
        else:
            print(f"   ⏭️  Skipped (too large). Copy manually if needed.\n")
            print(f"      rsync -av data/downloads/ {output}/downloads/\n")

    # ── 6. Write manifest ──
    manifest = {
        "timestamp": timestamp,
        "exported_from": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME", "unknown"),
        "contents": {
            "postgres_dump": str(db_dump.name),
            "sessions": [f.name for f in sessions_dir.glob("*.session")],
            "transcriptions": len(list((output / "transcriptions").glob("*.json"))) if (output / "transcriptions").exists() else 0,
            "labeled": len(list((output / "labeled").glob("*.json"))) if (output / "labeled").exists() else 0,
        }
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"📋 Manifest written: {manifest_path}")
    print(f"\n✅ Export complete! Copy '{output}' to USB stick.")
    print(f"   On the other machine run: python tools/sync/import_state.py --input /path/to/usb/bloger-bot-sync\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=f"./sync_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    args = parser.parse_args()
    export_state(args.output)