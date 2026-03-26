"""Export / import Chroma persistence from the docker-compose ``chromadb`` service volume.

Compose mounts named volume at ``/chroma/chroma`` (see docker-compose.dev.yml).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _run(cmd: str, *, cwd: Path, dry_run: bool) -> subprocess.CompletedProcess:
    print(f"  $ {cmd}")
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _chroma_volume_name(repo_root: Path, compose_file: str) -> str | None:
    r = _run(
        f'docker compose -f "{compose_file}" ps -a -q chromadb',
        cwd=repo_root,
        dry_run=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    cid = r.stdout.strip().splitlines()[-1].strip()
    if not cid:
        return None
    ir = subprocess.run(
        ["docker", "inspect", cid],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if ir.returncode != 0:
        return None
    data = json.loads(ir.stdout)[0]
    for m in data.get("Mounts", []):
        if m.get("Destination") == "/chroma/chroma" and m.get("Type") == "volume":
            return m.get("Name")
    return None


def export_chroma_volume(
    repo_root: Path,
    compose_file: str,
    dest_dir: Path,
    *,
    dry_run: bool,
) -> bool:
    """Copy ``chromadb:/chroma/chroma`` into ``dest_dir``. Returns False if skipped."""
    dest_q = str(dest_dir.resolve())
    # Ensure chromadb exists so cp works
    up = _run(
        f'docker compose -f "{compose_file}" up -d chromadb',
        cwd=repo_root,
        dry_run=dry_run,
    )
    if not dry_run and up.returncode != 0:
        print(f"   ⚠️  Could not start chromadb: {up.stderr.strip() or up.stdout.strip()}")
        return False

    if not dry_run and dest_dir.exists():
        shutil.rmtree(dest_dir)
    if not dry_run:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        dest_dir.mkdir(parents=True, exist_ok=True)

    cp = _run(
        f'docker compose -f "{compose_file}" cp chromadb:/chroma/chroma/. "{dest_q}"',
        cwd=repo_root,
        dry_run=dry_run,
    )
    if not dry_run and cp.returncode != 0:
        print(f"   ⚠️  chromadb cp failed: {cp.stderr.strip() or cp.stdout.strip()}")
        return False
    return True


def import_chroma_volume(
    repo_root: Path,
    compose_file: str,
    src_dir: Path,
    *,
    dry_run: bool,
) -> bool:
    """Replace Docker Chroma volume contents from ``src_dir`` (from a previous export)."""
    if not src_dir.is_dir():
        return False
    if not dry_run and not any(src_dir.iterdir()):
        print("   ⏭️  Chroma staging dir empty, skip import\n")
        return False

    vol = _chroma_volume_name(repo_root, compose_file)
    if not vol and not dry_run:
        _run(f'docker compose -f "{compose_file}" up -d chromadb', cwd=repo_root, dry_run=False)
        _run(f'docker compose -f "{compose_file}" stop chromadb', cwd=repo_root, dry_run=False)
        vol = _chroma_volume_name(repo_root, compose_file)
    if not vol and not dry_run:
        print("   ⚠️  Could not find chromadb volume after compose up.\n")
        return False

    _run(f'docker compose -f "{compose_file}" stop chromadb', cwd=repo_root, dry_run=dry_run)

    if vol and not dry_run:
        wipe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{vol}:/w",
                "alpine:3.20",
                "sh",
                "-c",
                "rm -rf /w/* 2>/dev/null; exit 0",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if wipe.returncode != 0:
            print(f"   ⚠️  Volume wipe (alpine) failed: {wipe.stderr.strip()}\n")

    src_q = str(src_dir.resolve())
    cp = _run(
        f'docker compose -f "{compose_file}" cp "{src_q}/." chromadb:/chroma/chroma/',
        cwd=repo_root,
        dry_run=dry_run,
    )
    if not dry_run and cp.returncode != 0:
        print(f"   ⚠️  chromadb import cp failed: {cp.stderr.strip() or cp.stdout.strip()}\n")
        _run(f'docker compose -f "{compose_file}" start chromadb', cwd=repo_root, dry_run=False)
        return False

    _run(f'docker compose -f "{compose_file}" start chromadb', cwd=repo_root, dry_run=dry_run)
    return True
