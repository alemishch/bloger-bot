# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Bloger Bot is a white-label Telegram bot platform for health/wellness bloggers. It provides RAG-powered Q&A, onboarding, diagnostics, and personalized content delivery. Full spec in `docs/TASK.md`, architecture in `docs/ARCHITECTURE.md`.

Currently implemented (Stage 1): content ingestion pipeline, LLM/RAG service, Telegram bot with onboarding, user-service API.

### Architecture (9 services)

| Service | Port | Purpose |
|---------|------|---------|
| postgres | 5432 | Primary DB (users, sessions, messages, content, onboarding) |
| redis | 6379 | Celery broker |
| chromadb | 8000 | Vector DB for RAG |
| ingestion-service | 8002 | Content pipeline API (parse, transcribe, label, vectorize) |
| ingestion-worker | — | Celery: label + vectorize (queue: default) |
| ingestion-transcription-worker | — | Celery: transcribe (queue: transcriptions) |
| llm-service | 8003 | RAG engine: `POST /api/v1/ask` |
| user-service | 8004 | User profiles, onboarding, sessions: `GET /api/v1/users/{tid}` |
| telegram-bot-yuri | — | aiogram 3.x bot @yuri_kinash_bot (polling mode) |

All services defined in `docker-compose.dev.yml`. Source code is volume-mounted for hot reload.

### Starting services

1. **Start Docker daemon** (Cloud Agent VMs only):
   ```
   sudo dockerd &>/dev/null &
   sleep 3
   sudo chmod 666 /var/run/docker.sock
   ```
2. **Start everything**: `docker compose -f docker-compose.dev.yml up -d --build`
3. **Run migrations** (new migration files must be copied into the container first):
   ```
   docker cp migrations/versions/ workspace-ingestion-service-1:/app/migrations/versions/
   docker exec workspace-ingestion-service-1 alembic -c /app/migrations/alembic.ini upgrade head
   ```
4. **Verify**:
   - `curl http://localhost:8002/health` — ingestion-service
   - `curl http://localhost:8003/health` — llm-service
   - `curl http://localhost:8004/health` — user-service
   - Bot logs: `docker compose -f docker-compose.dev.yml logs telegram-bot-yuri`

### Required secrets / env vars

- `OPENAI_API_KEY` — for LLM + RAG + embeddings
- `TELEGRAM_BOT_TOKEN_YURI` — bot token from @BotFather
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — for Pyrogram content parsing
- All set in `.env` (gitignored) or as Cursor Secrets

### White-label config

- Per-blogger config: `config/bloggers/{yuri,maria}.yaml` (token, Tone of Voice, RAG params, branding)
- Per-blogger onboarding: `config/onboarding/{yuri}.yaml` (step definitions, questions, lead magnets)
- `BLOGGER_ID` env var selects which config a service instance uses

### DB schema

Tables: `users`, `chat_sessions`, `chat_messages`, `onboarding_responses`, `content_sources`, `content_items`, `content_chunks`. Migrations in `migrations/versions/`. The ingestion-service Dockerfile copies but does NOT volume-mount the migrations dir — new migration files must be `docker cp`'d into the container before running `alembic upgrade`.

### Gotchas

- **SQL in bot/db.py**: Use `CAST(:param AS enumtype)` not `::enumtype` — the latter conflicts with SQLAlchemy's `:param` syntax.
- **Migrations**: Not volume-mounted. After creating a new migration file, `docker cp` it into the ingestion-service container before running alembic.
- **Makefile**: OS-detection helpers `_TIMESTAMP` / `_PG_PASSWORD` auto-switch between Linux and Windows. The `migrate` Compose service doesn't exist — use `docker exec` instead.
- **Redis password**: Comes from env var at container creation time. Check actual password with `docker inspect workspace-redis-1 | grep requirepass`.
- **Onboarding scenarios**: Defined in YAML (`config/onboarding/`). Swap by editing the YAML file — no code changes needed. Bot reads config at startup.

### Lint / Format

- `source .venv/bin/activate && ruff check .`
- `source .venv/bin/activate && ruff format .`

### Key API endpoints

**Ingestion** (port 8002): See `Makefile` for curl shortcuts. Swagger at `http://localhost:8002/docs`.

**LLM** (port 8003):
- `POST /api/v1/ask` — `{"query": "...", "blogger_id": "yuri"}` → RAG answer

**User** (port 8004):
- `GET /api/v1/users/{telegram_id}` — user profile
- `GET /api/v1/users/{telegram_id}/onboarding` — onboarding responses
- `GET /api/v1/users/{telegram_id}/sessions` — chat sessions
- `GET /api/v1/sessions/{session_id}/messages` — message history
