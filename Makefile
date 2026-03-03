.PHONY: help up down build logs ps migrate migrate-create parse transcribe status

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

# ── Database ──
migrate: ## Run database migrations
	cd migrations && alembic upgrade head

migrate-create: ## Create a migration: make migrate-create MSG="add content tables"
	cd migrations && alembic revision --autogenerate -m "$(MSG)"

migrate-down: ## Rollback last migration
	cd migrations && alembic downgrade -1

# ── Content Pipeline ──
create-source: ## Create a source: make create-source NAME="Yuri Channel" CHANNEL="@yuri_channel" BLOGGER="yuri"
	curl -s -X POST http://localhost:8002/api/v1/sources/ \
		-H "Content-Type: application/json" \
		-d '{"name":"$(NAME)","source_type":"telegram","blogger_id":"$(BLOGGER)","config":{"channel_id":"$(CHANNEL)"}}' | python -m json.tool

parse: ## Trigger parsing: make parse SOURCE_ID=<uuid>
	curl -s -X POST http://localhost:8002/api/v1/sources/$(SOURCE_ID)/parse | python -m json.tool

pipeline-stats: ## Show pipeline statistics
	curl -s http://localhost:8002/api/v1/jobs/stats | python -m json.tool

list-items: ## List content items: make list-items STATUS=downloaded
	curl -s "http://localhost:8002/api/v1/jobs/?status=$(STATUS)&limit=20" | python -m json.tool

# ── Transcriber (LOCAL) ──
transcriber-install: ## Install transcriber tool locally
	cd tools/transcriber && pip install -e ".[dev]"

transcribe-file: ## Transcribe a single file: make transcribe-file FILE=path/to/file.mp4
	transcriber transcribe $(FILE)

transcribe-process: ## Process all downloaded items from DB
	transcriber process --limit 10

transcribe-watch: ## Watch for new downloads and auto-transcribe
	transcriber watch --interval 30

transcribe-status: ## Show pipeline status
	transcriber status

# ── Development ──
lint: ## Run linter
	ruff check .

format: ## Format code
	ruff format .

health: ## Check all service health
	@echo "Ingestion: $$(curl -s http://localhost:8002/health | python -m json.tool 2>/dev/null || echo 'DOWN')"

# ── Sync (USB / multi-laptop) ──
export-state: ## Export DB + sessions + transcriptions for USB transfer
	python tools/sync/export_state.py --output ./sync_export

export-state-to: ## Export to specific path: make export-state-to PATH=/e/bloger-sync
	python tools/sync/export_state.py --output $(PATH)

import-state: ## Import from USB: make import-state PATH=/e/bloger-sync
	python tools/sync/import_state.py --input $(PATH)

db-dump: ## Quick DB dump to file
	docker compose -f docker-compose.dev.yml exec -T postgres \
		pg_dump -U bloger_bot bloger_bot > backup_$(shell date +%Y%m%d_%H%M%S).sql
	@echo "✅ Dumped to backup_*.sql"

db-restore: ## Restore DB from file: make db-restore FILE=backup_20241201.sql
	docker compose -f docker-compose.dev.yml exec -T postgres \
		psql -U bloger_bot bloger_bot < $(FILE)
	@echo "✅ Restored from $(FILE)"