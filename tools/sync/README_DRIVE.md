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
2. Copy session files (`.session`), `sessions/`, `data/transcriptions`, `data/labeled`, `data/downloads` (if &lt; 10 GB), `data/audio`, `data/exports`, `data/rag`, and any vector DB dirs (e.g. `chroma_db`, `vector_store`) into a staging dir.
3. Upload the staging dir to `gdrive:bloger-bot-sync` with rclone.

Options:

- `--dry-run` – Only build staging and print the rclone command (no upload).
- `--skip-db` – Do not dump PostgreSQL.
- `--max-downloads-gb 5` – Skip copying `data/downloads` if larger than 5 GB (default 10).
- `--keep-staging` – Leave `.drive_sync_staging` after upload (for debugging).

### Pull from Google Drive (on cloud agent or new machine)

```bash
python tools/sync/sync_from_drive.py
```

This will:

1. Download `gdrive:bloger-bot-sync` into `.drive_sync_staging`.
2. Restore DB (with confirmation), copy sessions, transcriptions, labeled, downloads, and extra paths into the repo.

Options:

- `--dry-run` – Only print what would be done.
- `--no-import` – Only download to `.drive_sync_staging`; do not apply import.
- `--yes-db` – Restore PostgreSQL without prompting.

---

## What gets synced

Synced (aligned with gitignore / export):

- PostgreSQL dump
- `*.session` and `sessions/`
- `data/transcriptions`, `data/labeled`, `data/downloads` (optional, size-limited), `data/audio`, `data/exports`, `data/rag`
- `chroma_db`, `vector_store`, `chromadb`, `qdrant_storage` (if present)

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
