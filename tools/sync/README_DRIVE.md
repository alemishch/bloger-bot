# Google Drive sync for cloud agents

Sync everything that’s in `.gitignore` (sessions, databases, media, RAG data) to **Google Drive** so you can use the same state on Cursor cloud agents or another machine. Uses **rclone** (free, no subscription).

- **Push (this machine):** `python tools/sync/sync_to_drive.py`
- **Pull (cloud agent / other machine):** `python tools/sync/sync_from_drive.py`

---

## One-time setup

### 1. Install rclone

- **Windows (scoop):** `scoop install rclone`  
- **Windows (winget):** `winget install Rclone.Rclone`  
- **macOS:** `brew install rclone`  
- **Linux:** `sudo apt install rclone` or [rclone.org/downloads](https://rclone.org/downloads/)

Check:

```bash
rclone version
```

### 2. Configure Google Drive remote

Run the interactive config and create a remote named `gdrive` (or change `remote_name` in `tools/sync/drive_sync_config.json`):

```bash
rclone config
```

- **n** – New remote  
- **name:** `gdrive`  
- **Storage:** `drive` (Google Drive)  
- **client_id / secret:** leave blank (Enter)  
- **scope:** `1` (Full access)  
- **root_folder_id:** leave blank  
- **service_account_file:** leave blank  
- **Edit advanced config?** `n`  
- **Use auto config?** `y` (opens browser to log in with Google)  
- **Configure as team drive?** `n`  
- **q** – Quit

Test:

```bash
rclone lsd gdrive:
```

You should see your Drive root folder listing.

### 3. (Optional) Change remote name or folder

Edit `tools/sync/drive_sync_config.json`:

- **remote_name:** must match the name you gave in `rclone config` (default `gdrive`).
- **remote_folder:** folder on Google Drive where state is stored (default `bloger-bot-sync`). It will be created on first push.

---

## Usage

Run from the **repo root** (where `docker-compose.dev.yml` is):

### Push to Google Drive (before using cloud / other machine)

```bash
python tools/sync/sync_to_drive.py
```

This will:

1. Dump PostgreSQL (if Docker is running) into the sync bundle.
2. Copy session files (`.session`), `sessions/`, `data/transcriptions`, `data/labeled`, extra paths from `drive_sync_config.json` (exports, RAG JSON, legacy vector dirs if present).
3. **Snapshot the Docker Chroma volume** (`chromadb` service → `chroma_docker_volume/` in staging) so embeddings travel with the bundle (requires `docker compose up -d chromadb`).
4. Upload the staging dir to `gdrive:bloger-bot-sync` with **`rclone sync`** (files removed from staging are **removed on Drive** on the next push).

**`data/downloads` is not synced by default** (large video/audio). Set `"include_downloads": true` in `drive_sync_config.json` or pass `--include-downloads` once if you really need it.

Options:

- `--dry-run` – Only build staging and print the rclone command (no upload).
- `--skip-db` – Do not dump PostgreSQL.
- `--include-downloads` – Copy `data/downloads` into staging (still subject to `--max-downloads-gb` when enabled in config).
- `--max-downloads-gb 5` – When downloads are enabled, skip copying `data/downloads` if larger than 5 GB (default 10).
- `--skip-chroma` – Omit the Docker Chroma volume export step.
- `--keep-staging` – Leave `.drive_sync_staging` after upload (for debugging).

### Pull from Google Drive (on cloud agent or new machine)

```bash
python tools/sync/sync_from_drive.py
```

This will:

1. Download `gdrive:bloger-bot-sync` into `.drive_sync_staging`.
2. Restore DB (with confirmation), copy sessions, transcriptions, labeled, optional downloads, extra paths into the repo.
3. **Import the Chroma snapshot** into the Docker `chromadb` named volume (stops `chromadb`, wipes volume contents, copies `chroma_docker_volume/`, starts `chromadb` again). Uses a short `alpine` container to clear the volume; requires Docker.

Options:

- `--dry-run` – Only print what would be done.
- `--no-import` – Only download to `.drive_sync_staging`; do not apply import.
- `--yes-db` – Restore PostgreSQL without prompting.
- `--overwrite-data` – When applying import, always replace files under `./data` from staging (default: only copy if staging file is newer).
- `--from-staging` – Skip rclone; run import only from existing `.drive_sync_staging` (useful after a download or to retry DB/file copy).
- `--include-downloads` – Restore `data/downloads` from staging even when config keeps downloads off.
- `--skip-chroma-import` – Do not replace the Docker Chroma volume (e.g. you only want Postgres + transcripts).

**Layout note:** `sync_to_drive` copies the same folders both at `transcriptions/` and `data/transcriptions/` (and the same for labeled/downloads). Pull previously only read the top-level folders, so if Drive had the canonical tree under `data/`, `./data` stayed stale. That is fixed; both layouts are merged into `./data/...`.

**DB restore:** Before `DROP DATABASE`, the import scripts terminate other sessions on `bloger_bot` (so ingestion-service connections do not block restore). If restore still fails, stop dependent containers with `docker compose -f docker-compose.dev.yml stop` and run import again.

**After DB restore:** Restart API and workers so they drop stale SQLAlchemy pools (otherwise you may see `connection is closed` / 500 on `POST /api/v1/sources/`):

`docker compose -f docker-compose.dev.yml restart ingestion-service ingestion-worker ingestion-download-worker ingestion-transcription-worker`

**After Chroma import:** Restart consumers so they reconnect to the DB-backed index:

`docker compose -f docker-compose.dev.yml restart chromadb llm-service telegram-bot-yuri`

---

## What gets synced

Configured in `tools/sync/drive_sync_config.json`:

- PostgreSQL dump
- `*.session` and `sessions/`
- `data/transcriptions`, `data/labeled`, `data/exports`, `data/rag`, other `paths` entries if the folders exist
- **`chroma_docker_volume/`** – snapshot of the **Docker** Chroma data directory (`chromadb:/chroma/chroma`), not the optional repo folders `chroma_db` / `vector_store` (those are still copied only if present on disk)

**Videos / `data/downloads`:**

- Default **`include_downloads`: false** — **no** upload or download of `data/downloads`, so large media are not pushed to Drive.
- **`rclone sync`** deletes remote files that disappear from staging. After one push **without** `downloads/` in staging, any old **`downloads/` tree on Google Drive (e.g. three videos) is removed** automatically.
- To opt in temporarily: `"include_downloads": true` or `--include-downloads` on push/pull.

**Not synced (on purpose):**

- `.env`, `secrets/`, credentials – keep these only on the machine that needs them (or use env vars on the cloud agent).

---

## Cloud agent workflow

1. **On your main machine:** run `python tools/sync/sync_to_drive.py` before starting work in the cloud.
2. **In the cloud agent:** clone the repo (or pull), then run `python tools/sync/sync_from_drive.py`. Use `--yes-db` if you want to restore the DB without a prompt.
3. Start Docker / services and run the pipeline as usual.
4. (Optional) After making changes in the cloud, run `sync_to_drive.py` again from the cloud if that environment has rclone configured, so your main machine can pull later.

---

## Troubleshooting

- **“rclone: command not found”** – Install rclone and ensure it’s on your PATH.
- **“Failed to create file system: didn’t find remote”** – Run `rclone config` and create a remote with the same name as `remote_name` in `drive_sync_config.json`.
- **DB dump empty / failed** – Start Docker first: `docker compose -f docker-compose.dev.yml up -d postgres`, then run sync again.
- **Google Drive quota** – Free accounts have 15 GB; watch size of `data/downloads`. Use `--max-downloads-gb` or exclude large media if needed.
