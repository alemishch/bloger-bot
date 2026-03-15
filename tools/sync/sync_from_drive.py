"""
Pull project state from Google Drive into this repo (e.g. on a cloud agent or new machine).
Uses rclone; run sync_to_drive.py on your main machine first.

Usage (from repo root):
    python tools/sync/sync_from_drive.py
    python tools/sync/sync_from_drive.py --dry-run
    python tools/sync/sync_from_drive.py --no-import  (only download, do not apply import)
"""
import os
import subprocess
import shutil
import json
import argparse
from pathlib import Path


def run(cmd: str, check: bool = False, dry_run: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {cmd}")
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, None, None)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        if result.stderr:
            print(f"  ⚠️  stderr: {result.stderr.strip()}")
        if check:
            raise RuntimeError(f"Command failed: {cmd}")
    return result


def ensure_rclone() -> None:
    """Check rclone is on PATH; raise with install instructions if not."""
    kwargs = {"capture_output": True, "timeout": 5}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.run(["rclone", "version"], **kwargs)
    except FileNotFoundError:
        raise RuntimeError(
            "rclone is not installed or not on your PATH.\n"
            "Install it first:\n"
            "  • Windows (winget): winget install Rclone.Rclone\n"
            "  • Windows (scoop):  scoop install rclone\n"
            "  • Then close and reopen your terminal, and run: rclone config\n"
            "  See tools/sync/README_DRIVE.md for full setup."
        )
    except subprocess.TimeoutExpired:
        pass  # rclone exists but hung; let sync fail with its own error


def find_repo_root() -> Path:
    cur = Path(os.path.abspath(os.curdir))
    for _ in range(10):
        if (cur / "docker-compose.dev.yml").exists() or (cur / ".git").exists():
            return cur
        cur = cur.parent
    return Path(os.path.abspath(os.curdir))


def load_config(repo_root: Path) -> dict:
    config_path = repo_root / "tools" / "sync" / "drive_sync_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def apply_import(repo_root: Path, source: Path, confirm_db: bool = True, dry_run: bool = False):
    """Apply imported state (same logic as import_state.py)."""
    # ── 1. Restore PostgreSQL ──
    db_dump = source / "postgres_dump.sql"
    if db_dump.exists():
        print("1️⃣  Restoring PostgreSQL...")
        if confirm_db and not dry_run:
            confirm = input("   Overwrite local DB? [y/N]: ").strip().lower()
            if confirm != "y":
                print("   ⏭️  Skipped DB restore\n")
            else:
                run(
                    'docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -c "DROP DATABASE IF EXISTS bloger_bot_backup;"',
                    dry_run=dry_run,
                )
                run(
                    f'docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d bloger_bot < "{db_dump}"',
                    check=False,
                    dry_run=dry_run,
                )
                print("   ✅ Database restored\n")
        elif dry_run:
            print("   (dry-run) Would restore DB\n")
        else:
            run(
                f'docker compose -f docker-compose.dev.yml exec -T postgres psql -U bloger_bot -d bloger_bot < "{db_dump}"',
                check=False,
                dry_run=dry_run,
            )
            print("   ✅ Database restored\n")
    else:
        print("1️⃣  ⏭️  No DB dump in sync\n")

    # ── 2. Session files ──
    sessions_src = source / "sessions"
    if sessions_src.exists():
        print("2️⃣  Copying session files...")
        if not dry_run:
            (repo_root / "sessions").mkdir(exist_ok=True)
        for sf in sessions_src.glob("*.session"):
            if not dry_run:
                shutil.copy2(sf, repo_root / sf.name)
                shutil.copy2(sf, repo_root / "sessions" / sf.name)
            print(f"   ✅ {sf.name} → ./ and sessions/")
        print()
    else:
        print("2️⃣  ⏭️  No session files\n")

    # ── 3. Transcriptions ──
    trans_src = source / "transcriptions"
    if trans_src.exists():
        print("3️⃣  Copying transcriptions...")
        trans_dst = repo_root / "data" / "transcriptions"
        trans_dst.mkdir(parents=True, exist_ok=True)
        for f in trans_src.glob("*.json"):
            dest = trans_dst / f.name
            if not dry_run and (not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime):
                shutil.copy2(f, dest)
            print(f"   ✅ {f.name}")
        print()
    else:
        print("3️⃣  ⏭️  No transcriptions\n")

    # ── 4. Labeled ──
    labeled_src = source / "labeled"
    if labeled_src.exists():
        print("4️⃣  Copying labeled data...")
        labeled_dst = repo_root / "data" / "labeled"
        labeled_dst.mkdir(parents=True, exist_ok=True)
        for f in labeled_src.glob("*.json"):
            dest = labeled_dst / f.name
            if not dry_run and (not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime):
                shutil.copy2(f, dest)
            print(f"   ✅ {f.name}")
        print()
    else:
        print("4️⃣  ⏭️  No labeled data\n")

    # ── 5. Downloads ──
    downloads_src = source / "downloads"
    if downloads_src.exists():
        print("5️⃣  Copying downloads...")
        downloads_dst = repo_root / "data" / "downloads"
        downloads_dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in downloads_src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(downloads_src)
                dest = downloads_dst / rel
                if not dry_run and (not dest.exists() or f.stat().st_mtime > dest.stat().st_mtime):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    copied += 1
        print(f"   ✅ {copied} files\n")
    else:
        print("5️⃣  ⏭️  No downloads\n")

    # ── 6. Extra: data/audio, data/exports, data/rag, chroma_db, vector_store, etc. ──
    for rel in ["data/audio", "data/exports", "data/rag", "chroma_db", "vector_store", "chromadb", "qdrant_storage"]:
        src = source / rel
        if not src.exists():
            continue
        print(f"   Copying {rel}...")
        dst = repo_root / rel
        dst.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            for f in src.rglob("*"):
                if f.is_file():
                    d = dst / f.relative_to(src)
                    d.parent.mkdir(parents=True, exist_ok=True)
                    if not d.exists() or f.stat().st_mtime > d.stat().st_mtime:
                        shutil.copy2(f, d)
        print(f"   ✅ {rel}\n")


def main():
    parser = argparse.ArgumentParser(description="Pull project state from Google Drive.")
    parser.add_argument("--dry-run", action="store_true", help="Only print rclone command and import steps.")
    parser.add_argument("--no-import", action="store_true", help="Only download to staging; do not apply import.")
    parser.add_argument("--yes-db", action="store_true", help="Restore DB without prompting.")
    args = parser.parse_args()

    repo_root = find_repo_root()
    os.chdir(repo_root)
    ensure_rclone()
    config = load_config(repo_root)
    remote = config["remote_name"]
    folder = config["remote_folder"]
    staging = repo_root / ".drive_sync_staging"

    dest = f"{remote}:{folder}"
    print(f"\n📥 Sync from Google Drive: {dest}")
    print(f"   Repo root: {repo_root}\n")

    # rclone sync remote:folder → staging
    if staging.exists() and not args.dry_run:
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    cmd = f'rclone sync "{dest}" "{staging}" --progress -v'
    run(cmd, check=True, dry_run=args.dry_run)
    if args.dry_run:
        print(f"   (dry-run) Would run: {cmd}\n")
        return

    manifest_path = staging / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"\n📋 Synced state from {manifest.get('timestamp', '?')} ({manifest.get('exported_from', '?')})\n")
    else:
        print("\n📋 No manifest in remote (empty or first run).\n")

    if not args.no_import:
        apply_import(repo_root, staging, confirm_db=not args.yes_db, dry_run=args.dry_run)

    if not args.no_import and not args.dry_run:
        print("✅ Import complete. You can run the pipeline (e.g. make up, make pipeline-stats).\n")
    else:
        print(f"✅ Downloaded to {staging}. Run with --no-import to only download.\n")


if __name__ == "__main__":
    main()
