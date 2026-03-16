.PHONY: help up down build logs ps migrate migrate-create migrate-down \
		up-infra parse pipeline-stats list-items create-source \
		transcriber-install transcribe-file transcribe-process transcribe-watch transcribe-status \
		export-state export-state-to import-state db-dump db-restore health lint format

# ── OS helpers ──
ifeq ($(OS),Windows_NT)
  _TIMESTAMP = $(shell powershell -Command "Get-Date -Format 'yyyyMMdd_HHmmss'")
  _PG_PASSWORD = $(shell powershell -Command "(Get-Content .env | Select-String 'POSTGRES_PASSWORD').ToString().Split('=')[1]")
else
  _TIMESTAMP = $(shell date +%Y%m%d_%H%M%S)
  _PG_PASSWORD = $(shell grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2)
endif

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

# ── Docker ──
up: ## Start all services
	docker compose -f docker-compose.dev.yml up -d

up-infra: ## Start only infrastructure (postgres, redis, chromadb)
	docker compose -f docker-compose.dev.yml up -d postgres redis chromadb

down: ## Stop all services
	docker compose -f docker-compose.dev.yml down

build: ## Build all Docker images
	docker compose -f docker-compose.dev.yml build

logs: ## Follow all logs
	docker compose -f docker-compose.dev.yml logs -f

logs-worker: ## Follow ingestion worker logs
	docker compose -f docker-compose.dev.yml logs -f ingestion-worker

ps: ## Show running services
	docker compose -f docker-compose.dev.yml ps

# ── Database / Migrations ──
migrate: ## Run database migrations
	docker compose -f docker-compose.dev.yml --profile migrate run --rm migrate \
		alembic -c /app/migrations/alembic.ini upgrade head

migrate-create: ## Create migration: make migrate-create MSG="add users table"
	docker compose -f docker-compose.dev.yml --profile migrate run --rm migrate \
		alembic -c /app/migrations/alembic.ini revision --autogenerate -m "$(MSG)"

migrate-down: ## Rollback last migration
	docker compose -f docker-compose.dev.yml --profile migrate run --rm migrate \
		alembic -c /app/migrations/alembic.ini downgrade -1

migrate-current: ## Show current migration state
	docker compose -f docker-compose.dev.yml --profile migrate run --rm migrate \
		alembic -c /app/migrations/alembic.ini current

migrate-history: ## Show migration history
	docker compose -f docker-compose.dev.yml --profile migrate run --rm migrate \
		alembic -c /app/migrations/alembic.ini history


db-dump: ## Quick DB dump to file
	docker compose -f docker-compose.dev.yml exec postgres \
		pg_dump -U $${POSTGRES_USER:-bloger_bot} $${POSTGRES_DB:-bloger_bot} \
		> backup_$(_TIMESTAMP).sql
	@echo "✅ Dumped"

db-restore: ## Restore DB: make db-restore FILE=backup.sql
	docker compose -f docker-compose.dev.yml exec -T postgres \
		psql -U $${POSTGRES_USER:-bloger_bot} $${POSTGRES_DB:-bloger_bot} < $(FILE)
	@echo "✅ Restored from $(FILE)"

# ── Content Pipeline ──
create-source: ## make create-source NAME="Yuri Chat" CHANNEL="-100123" BLOGGER="yuri"
	curl -s -X POST http://localhost:8002/api/v1/sources/ \
		-H "Content-Type: application/json" \
		-d "{\"name\":\"$(NAME)\",\"source_type\":\"telegram\",\"blogger_id\":\"$(BLOGGER)\",\"config\":{\"channel_id\":\"$(CHANNEL)\"}}" \
		| python -m json.tool

parse: ## Trigger parsing: make parse SOURCE_ID=<uuid>
	curl -s -X POST http://localhost:8002/api/v1/sources/$(SOURCE_ID)/parse | python -m json.tool

pipeline-stats: ## Show pipeline statistics
	curl -s http://localhost:8002/api/v1/jobs/stats | python -m json.tool

list-items: ## List items: make list-items STATUS=downloaded
	curl -s "http://localhost:8002/api/v1/jobs/?status=$(STATUS)&limit=20" | python -m json.tool

# ── Transcriber (LOCAL) ──
transcriber-install: ## Install transcriber locally
	cd tools/transcriber && pip install -e ".[dev]"

transcribe-file: ## make transcribe-file FILE=path/to/file.mp4
	transcriber transcribe $(FILE)

transcribe-process: ## Process downloaded items from DB
	POSTGRES_HOST=localhost POSTGRES_PASSWORD=$(_PG_PASSWORD) transcriber process --limit 10

transcribe-watch: ## Watch and auto-transcribe
	POSTGRES_HOST=localhost POSTGRES_PASSWORD=$(_PG_PASSWORD) transcriber watch --interval 30

transcribe-watch-windows: ## Watch and auto-transcribe (Windows PowerShell)
	powershell -Command "Set-Item Env:POSTGRES_HOST 'localhost'; Set-Item Env:POSTGRES_PASSWORD '1122345'; transcriber watch --interval 30"
	
transcribe-status: ## Show pipeline status via API
	transcriber status

reset-failed-transcriptions: ## Reset failed transcriptions back to downloaded
	curl -s -X POST "http://localhost:8002/api/v1/jobs/retry-failed?status=transcription_failed" | python -m json.tool

queue-downloads: ## Queue all discovered items for download
	curl -s -X POST "http://localhost:8002/api/v1/jobs/queue-discovered?limit=500" | python -m json.tool

queue-labeling: ## Queue all transcribed items for labeling  
	curl -s -X POST "http://localhost:8002/api/v1/jobs/queue-transcribed?limit=500" | python -m json.tool

# ── Sync (USB / multi-laptop) ──
export-state: ## Export state to ./sync_export
	python tools/sync/export_state.py --output ./sync_export

export-state-to: ## make export-state-to PATH=E:/bloger-sync
	python tools/sync/export_state.py --output $(PATH)

import-state: ## make import-state PATH=E:/bloger-sync
	python tools/sync/import_state.py --input $(PATH)

# ── Dev ──
health: ## Check service health
	@curl -sf http://localhost:8002/health && echo " ✅ ingestion-service OK" || echo " ❌ ingestion-service DOWN"

lint: ## Run linter
	ruff check .

format: ## Format code
	ruff format .

queue-transcriptions: ## Queue all downloaded items for conversion+transcription
	curl -s -X POST "http://localhost:8002/api/v1/jobs/queue-downloaded?limit=500" | python -m json.tool

retry-failed: ## Retry all download_failed items
	curl -s -X POST "http://localhost:8002/api/v1/jobs/retry-failed-downloads?limit=500" | python -m json.tool

logs-transcription-worker: ## Follow transcription worker logs
	docker compose -f docker-compose.dev.yml logs -f ingestion-transcription-worker

stats-watch: ## Watch pipeline stats every 15s
ifeq ($(OS),Windows_NT)
	powershell -Command "while(1) { Clear-Host; Write-Host (Get-Date); curl.exe -s http://localhost:8002/api/v1/jobs/stats | python -m json.tool; Start-Sleep 15 }"
else
	watch -n 15 'echo "--- $$(date) ---"; curl -s http://localhost:8002/api/v1/jobs/stats | python3 -m json.tool'
endif

retry-failed-transcriptions: ## Retry transcription_failed (re-download corrupt + re-convert others)
	curl -s -X POST "http://localhost:8002/api/v1/jobs/retry-failed-transcriptions" | python -m json.tool

retry-stuck-chunking: ## Reset stuck chunking items and re-queue vectorization
	curl -s -X POST "http://localhost:8002/api/v1/jobs/retry-stuck-chunking" | python -m json.tool

recover-all: ## Recover ALL stuck/failed items in one shot
	curl -s -X POST "http://localhost:8002/api/v1/jobs/recover-all" | python3 -m json.tool

# ── Google Drive Sync ──
sync-from-drive: ## Pull state from Google Drive
	python tools/sync/sync_from_drive.py --yes-db

sync-to-drive: ## Push state to Google Drive
	python tools/sync/sync_to_drive.py