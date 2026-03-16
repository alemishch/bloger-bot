# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Bloger Bot is a content ingestion and RAG pipeline for Russian-language bloggers. It scrapes Telegram channels, processes media through download/transcribe/label/vectorize stages, and stores results in ChromaDB for semantic search. Only the **ingestion-service** (FastAPI on port 8002) is implemented; other planned microservices are not yet in the repo.

### Architecture

- **Infrastructure**: PostgreSQL 16, Redis 7, ChromaDB 0.5.23 — all run via `docker compose -f docker-compose.dev.yml`
- **Application**: FastAPI ingestion-service + 3 Celery workers (default, downloads, transcriptions)
- **Python**: Requires >=3.11. Two editable packages: `libs/common` and `services/ingestion-service`

### Starting services

1. **Start Docker daemon** (required in Cloud Agent VMs):
   ```
   sudo dockerd &>/dev/null &
   sleep 3
   sudo chmod 666 /var/run/docker.sock
   ```
2. **Start infrastructure**: `docker compose -f docker-compose.dev.yml up -d postgres redis chromadb`
3. **Build + start app**: `docker compose -f docker-compose.dev.yml up -d --build ingestion-service`
4. **Run migrations**: `docker exec workspace-ingestion-service-1 alembic -c /app/migrations/alembic.ini upgrade head`
5. **Verify**: `curl http://localhost:8002/health` should return `{"status":"ok","service":"ingestion-service"}`

### Gotchas

- The `Makefile` references a `migrate` Docker Compose service that does not exist in `docker-compose.dev.yml`. Run migrations via `docker exec` on the ingestion-service container instead.
- The `Makefile` has some Windows-specific commands (PowerShell) for `db-dump`, `transcribe-process`, etc. These won't work on Linux.
- The `PYTHONPATH` for local development must include both `libs/common/src` and `services/ingestion-service/src`.
- A Python virtualenv at `.venv/` is used for local linting and tooling. Activate with `source .venv/bin/activate`.
- Docker daemon in Cloud Agent VMs needs `fuse-overlayfs` storage driver and `iptables-legacy`. See the Docker setup in `.cursor/environment` snapshot.

### Lint / Format / Test

- **Lint**: `source .venv/bin/activate && ruff check .` (28 pre-existing warnings, all in the original codebase)
- **Format**: `source .venv/bin/activate && ruff format .`
- **Tests**: `tests/test_pipeline.py` and `tests/test_rag.py` are integration scripts that require live PostgreSQL + ChromaDB + optional OpenAI API key. They are run as standalone scripts, not via pytest.

### Key API endpoints

See `Makefile` for curl shortcuts. Core endpoints:
- `GET /health` — service health
- `POST /api/v1/sources/` — create content source
- `GET /api/v1/sources/` — list sources
- `GET /api/v1/jobs/stats` — pipeline statistics
- `POST /api/v1/sources/{source_id}/parse` — trigger Telegram channel parsing
- FastAPI docs at `http://localhost:8002/docs`
