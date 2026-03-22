"""
Import state exported from another machine.
Run this AFTER copying USB contents, BEFORE starting work.

Usage:
    python tools/sync/import_state.py --input /path/to/usb/bloger-bot-sync
"""
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


def _bundle_subdirs(source: Path, name: str) -> list[Path]:
    """USB/Drive bundle may use ``data/<name>`` only or top-level ``<name>`` (legacy)."""
    return [p for p in (source / "data" / name, source / name) if p.exists()]


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
            # Terminate app connections so DROP DATABASE succeeds (ingestion-service, etc.)
            run(
                'docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = \'bloger_bot\' AND pid <> pg_backend_pid();"'
            )
            run('docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d postgres -c "DROP DATABASE IF EXISTS bloger_bot;"')
            run('docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d postgres -c "CREATE DATABASE bloger_bot;"')
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
    trans_roots = _bundle_subdirs(source, "transcriptions")
    if trans_roots:
        print("3️⃣  Copying transcriptions...")
        trans_dst = Path("data/transcriptions")
        trans_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for trans_src in trans_roots:
            for f in trans_src.glob("*.json"):
                dest = trans_dst / f.name
                if not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime:
                    shutil.copy2(f, dest)
                    copied += 1
        total = sum(len(list(r.glob("*.json"))) for r in trans_roots)
        print(f"   ✅ {copied} file(s) updated / {total} seen in bundle\n")
    else:
        print("3️⃣  ⏭️  No transcriptions found\n")

    # ── 4. Copy labeled data ──
    labeled_roots = _bundle_subdirs(source, "labeled")
    if labeled_roots:
        print("4️⃣  Copying labeled data...")
        labeled_dst = Path("data/labeled")
        labeled_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for labeled_src in labeled_roots:
            for f in labeled_src.glob("*.json"):
                dest = labeled_dst / f.name
                if not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime:
                    shutil.copy2(f, dest)
                    copied += 1
        total = sum(len(list(r.glob("*.json"))) for r in labeled_roots)
        print(f"   ✅ {copied} file(s) updated / {total} seen in bundle\n")
    else:
        print("4️⃣  ⏭️  No labeled data found\n")

    # ── 5. Copy downloads if present (preserve directory structure) ──
    dl_roots = _bundle_subdirs(source, "downloads")
    if dl_roots:
        print("5️⃣  Copying downloads...")
        downloads_dst = Path("data/downloads")
        downloads_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for downloads_src in dl_roots:
            for f in downloads_src.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(downloads_src)
                    dest = downloads_dst / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime:
                        shutil.copy2(f, dest)
                        copied += 1
        print(f"   ✅ {copied} media file(s) updated\n")

    # ── 6. Restore ChromaDB (vector store) if present ──
    chroma_backup = source / "chroma_backup.tar.gz"
    if chroma_backup.exists():
        print("6️⃣  Restoring ChromaDB...")
        import tarfile
        chroma_restore = source / "_chroma_restore_temp"
        try:
            chroma_restore.mkdir(exist_ok=True)
            with tarfile.open(chroma_backup, "r:gz") as tf:
                tf.extractall(chroma_restore)
            restore_abs = str(chroma_restore.resolve())
            # Stop chromadb so we can write to its volume; then run copy into volume
            run("docker compose -f docker-compose.dev.yml stop chromadb")
            # Tar has top-level "chroma" dir from export, so copy /restore/chroma/. into volume
            run(
                f'docker compose -f docker-compose.dev.yml run --rm -v "{restore_abs}:/restore:ro" '
                '--entrypoint "" chromadb sh -c "rm -rf /chroma/chroma/* 2>/dev/null; cp -a /restore/chroma/. /chroma/chroma/"'
            )
            run("docker compose -f docker-compose.dev.yml start chromadb")
            print("   ✅ ChromaDB restored\n")
        except Exception as e:
            print(f"   ⚠️  ChromaDB restore failed: {e}\n")
        finally:
            if chroma_restore.exists():
                shutil.rmtree(chroma_restore, ignore_errors=True)
    else:
        print("6️⃣  ⏭️  No ChromaDB backup found, skipping\n")

    print("✅ Import complete! You can now run the pipeline.")
    print("   Run: make up  (if not already running)")
    print("   Run: make pipeline-stats  (to verify state)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    import_state(args.input)