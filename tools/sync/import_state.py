"""
Import state exported from another machine.
Run this AFTER copying USB contents, BEFORE starting work.

Usage:
    python tools/sync/import_state.py --input /path/to/usb/bloger-bot-sync
"""
import os
import subprocess
import shutil
import json
import argparse
from pathlib import Path


def run(cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ⚠️  stderr: {result.stderr.strip()}")
        if check:
            raise RuntimeError(f"Command failed: {cmd}")
    return result


def import_state(input_dir: str):
    source = Path(input_dir)
    if not source.exists():
        print(f"❌ Source directory not found: {input_dir}")
        return

    # Read manifest
    manifest_path = source / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        print(f"\n📦 Importing state exported at {manifest['timestamp']}")
        print(f"   Originally from: {manifest['exported_from']}\n")
    else:
        print(f"\n📦 Importing from {input_dir} (no manifest found)\n")

    # ── 1. Restore PostgreSQL ──
    db_dump = source / "postgres_dump.sql"
    if db_dump.exists():
        print("1️⃣  Restoring PostgreSQL...")
        print("   ⚠️  This will DROP and recreate the database!")
        confirm = input("   Continue? [y/N]: ").strip().lower()
        if confirm == "y":
            # Drop and recreate
            run('docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -c "DROP DATABASE IF EXISTS bloger_bot_backup;"')
            run(f'docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d bloger_bot < "{db_dump}"')
            print("   ✅ Database restored\n")
        else:
            print("   ⏭️  Skipped DB restore\n")
    else:
        print("1️⃣  ⏭️  No DB dump found, skipping\n")

    # ── 2. Copy session files ──
    sessions_src = source / "sessions"
    if sessions_src.exists():
        print("2️⃣  Copying session files...")
        # Copy to root
        for sf in sessions_src.glob("*.session"):
            shutil.copy2(sf, Path(".") / sf.name)
            print(f"   ✅ {sf.name} → ./{sf.name}")
        # Copy to sessions/ dir
        sessions_dst = Path("sessions")
        sessions_dst.mkdir(exist_ok=True)
        for sf in sessions_src.glob("*.session"):
            shutil.copy2(sf, sessions_dst / sf.name)
            print(f"   ✅ {sf.name} → sessions/{sf.name}")
        print()
    else:
        print("2️⃣  ⏭️  No session files found\n")

    # ── 3. Copy transcriptions ──
    trans_src = source / "transcriptions"
    if trans_src.exists():
        print("3️⃣  Copying transcriptions...")
        trans_dst = Path("data/transcriptions")
        trans_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in trans_src.glob("*.json"):
            dest = trans_dst / f.name
            if not dest.exists():  # don't overwrite newer local files
                shutil.copy2(f, dest)
                copied += 1
        total = len(list(trans_src.glob("*.json")))
        print(f"   ✅ {copied} new / {total} total transcriptions copied\n")
    else:
        print("3️⃣  ⏭️  No transcriptions found\n")

    # ── 4. Copy labeled data ──
    labeled_src = source / "labeled"
    if labeled_src.exists():
        print("4️⃣  Copying labeled data...")
        labeled_dst = Path("data/labeled")
        labeled_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in labeled_src.glob("*.json"):
            dest = labeled_dst / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                copied += 1
        total = len(list(labeled_src.glob("*.json")))
        print(f"   ✅ {copied} new / {total} total labeled files copied\n")
    else:
        print("4️⃣  ⏭️  No labeled data found\n")

    # ── 5. Copy downloads if present ──
    downloads_src = source / "downloads"
    if downloads_src.exists():
        print("5️⃣  Copying downloads...")
        downloads_dst = Path("data/downloads")
        downloads_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in downloads_src.rglob("*"):
            if f.is_file():
                dest = downloads_dst / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                    copied += 1
        print(f"   ✅ {copied} new media files copied\n")

    print("✅ Import complete! You can now run the pipeline.")
    print("   Run: make up  (if not already running)")
    print("   Run: make pipeline-stats  (to verify state)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    import_state(args.input)