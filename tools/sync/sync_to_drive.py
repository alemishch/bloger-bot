"""
Push gitignored project state to Google Drive for use on cloud agents or another machine.
Uses rclone (must be installed and configured once; see README_DRIVE.md).

Usage (from repo root):
    python tools/sync/sync_to_drive.py
    python tools/sync/sync_to_drive.py --dry-run
    python tools/sync/sync_to_drive.py --skip-db
"""
import os
import subprocess
import shutil
import json
import argparse
from datetime import datetime
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
        pass


def find_repo_root() -> Path:
    """Repo root: directory containing docker-compose.dev.yml or .git."""
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


def build_staging(repo_root: Path, staging: Path, config: dict, skip_db: bool, dry_run: bool, max_downloads_gb: float):
    """Build staging directory with all paths to sync (same layout as export_state + extra paths)."""
    staging.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── 1. PostgreSQL dump ──
    if config.get("include_postgres_dump", True) and not skip_db:
        print("1️⃣  Dumping PostgreSQL...")
        db_dump = staging / "postgres_dump.sql"
        if not dry_run:
            run(
                f'docker compose -f docker-compose.dev.yml exec -T postgres '
                f'pg_dump -U bloger_bot bloger_bot > "{db_dump}"',
                check=False,
                dry_run=dry_run,
            )
        if db_dump.exists() and db_dump.stat().st_size > 0:
            print(f"   ✅ DB dump: {db_dump.stat().st_size // 1024} KB\n")
        else:
            print("   ⏭️  No DB dump (docker not running or skip-db)\n")
    else:
        print("1️⃣  Skipping PostgreSQL dump\n")

    # ── 2. Session files ──
    print("2️⃣  Copying session files...")
    sessions_dir = staging / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    session_files = (
        list(repo_root.glob("*.session"))
        + list((repo_root / "tools").glob("*.session"))
        + (list((repo_root / "sessions").glob("*.session")) if (repo_root / "sessions").exists() else [])
    )
    for sf in session_files:
        dest = sessions_dir / sf.name
        if not dry_run:
            shutil.copy2(sf, dest)
        print(f"   ✅ {sf.relative_to(repo_root)} → sessions/{dest.name}")
    print()

    # ── 3–4. transcriptions, labeled ──
    for label, src_name, dst_name in [
        ("3️⃣  Copying transcriptions", "data/transcriptions", "transcriptions"),
        ("4️⃣  Copying labeled data", "data/labeled", "labeled"),
    ]:
        print(label + "...")
        src = repo_root / src_name
        if src.exists():
            dst = staging / dst_name
            if dst.exists() and not dry_run:
                shutil.rmtree(dst)
            if not dry_run:
                shutil.copytree(src, dst)
            count = len(list((dst if dst.exists() else src).rglob("*"))) if not dry_run else 0
            print(f"   ✅ {dst_name}\n")
        else:
            print(f"   ⏭️  No {src_name}\n")

    # ── 5. Downloads (respect size limit) ──
    print("5️⃣  Downloads...")
    downloads_src = repo_root / "data" / "downloads"
    if downloads_src.exists():
        total_size = sum(f.stat().st_size for f in downloads_src.rglob("*") if f.is_file())
        total_gb = total_size / (1024 ** 3)
        if total_gb < max_downloads_gb:
            downloads_dst = staging / "downloads"
            if downloads_dst.exists() and not dry_run:
                shutil.rmtree(downloads_dst)
            if not dry_run:
                shutil.copytree(downloads_src, downloads_dst)
            print(f"   ✅ {total_gb:.2f} GB copied\n")
        else:
            print(f"   ⏭️  Skipped ({total_gb:.2f} GB > {max_downloads_gb} GB). Use --max-downloads-gb to change.\n")
    else:
        print("   ⏭️  No data/downloads\n")

    # ── 6. Extra paths from config (data/audio, data/exports, data/rag, chroma_db, etc.) ──
    handled = {"sessions", "data/transcriptions", "data/labeled", "data/downloads"}
    extra = [p for p in config.get("paths", []) if p not in handled]
    for i, rel in enumerate(extra, start=6):
        print(f"{i}️⃣  {rel}...")
        src = repo_root / rel
        if src.exists() and src.is_dir():
            dst = staging / rel
            if dst.exists() and not dry_run:
                shutil.rmtree(dst)
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
            print(f"   ✅ {rel}\n")
        else:
            print(f"   ⏭️  Not present\n")

    # ── Manifest ──
    manifest = {
        "timestamp": timestamp,
        "exported_from": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME", "unknown"),
        "sync_target": "google_drive",
        "contents": {},
    }
    for d in staging.iterdir():
        if d.is_dir():
            n = len(list(d.rglob("*")))
            manifest["contents"][d.name] = n
        elif d.is_file():
            manifest["contents"][d.name] = 1
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print("📋 Manifest written.\n")


def main():
    parser = argparse.ArgumentParser(description="Push project state to Google Drive (rclone).")
    parser.add_argument("--dry-run", action="store_true", help="Build staging and print rclone command only.")
    parser.add_argument("--skip-db", action="store_true", help="Do not dump PostgreSQL.")
    parser.add_argument("--max-downloads-gb", type=float, default=10.0, help="Skip copying data/downloads if larger (default 10).")
    parser.add_argument("--keep-staging", action="store_true", help="Do not delete staging after sync.")
    args = parser.parse_args()

    repo_root = find_repo_root()
    os.chdir(repo_root)
    ensure_rclone()
    config = load_config(repo_root)
    remote = config["remote_name"]
    folder = config["remote_folder"]
    staging = repo_root / ".drive_sync_staging"

    print(f"\n📤 Sync to Google Drive: {remote}:{folder}")
    print(f"   Repo root: {repo_root}\n")

    build_staging(repo_root, staging, config, args.skip_db, args.dry_run, args.max_downloads_gb)

    # rclone sync staging → remote:folder
    dest = f"{remote}:{folder}"
    cmd = f'rclone sync "{staging}" "{dest}" --progress -v'
    run(cmd, check=True, dry_run=args.dry_run)
    if args.dry_run:
        print(f"   (dry-run) Would run: {cmd}\n")
        return

    if not args.keep_staging:
        print("   Removing staging dir...")
        shutil.rmtree(staging, ignore_errors=True)
    print(f"\n✅ Synced to {dest}\n")
    print("   On cloud agent / other machine run: python tools/sync/sync_from_drive.py\n")


if __name__ == "__main__":
    main()
