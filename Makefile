.PHONY: help up down build logs ps

help: 
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

up: 
	docker compose up -d

up-infra: 

down:
	docker compose down

build: 
	docker compose build

logs: 
	docker compose logs -f

ps: 
	docker compose ps

# ── Ingestion ──
parse: 
	curl -X POST http://localhost:8002/api/v1/sources/$(SOURCE_ID)/parse

# ── Transcriber (local) ──
transcriber-install:
	cd tools/transcriber && pip install -e .

transcribe: ## Transcribe a single file: make transcribe FILE=path/to/file.mp4
	transcriber transcribe $(FILE)

transcribe-batch: ## Batch transcribe all pending downloads
	transcriber batch

transcribe-watch: ## Watch for new downloads and auto-transcribe
	transcriber watch

transcribe-status: ## Show transcription job statuses
	transcriber status

# ── Database ──
migrate: ## Run database migrations
	cd migrations && alembic upgrade head

migrate-create: ## Create a new migration: make migrate-create MSG="description"
	cd migrations && alembic revision --autogenerate -m "$(MSG)"

# ── Development ──
lint: 
	ruff check .

format: 
	ruff format .
