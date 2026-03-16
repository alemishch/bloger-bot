# Bloger Bot — Architecture Plan

## Overview

Two independent Telegram bots (Yuri + Maria) on a shared white-label codebase.
Each bot has its own token, content DB, branding — but runs on the same infrastructure.

## Stage 1 Scope (current)

Infrastructure + RAG + minimal bot that answers questions using the knowledge base.

### Services

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ telegram-bot  │  │ llm-service  │  │  user-service │  │
│  │  (aiogram)    │──│  (FastAPI)   │  │  (FastAPI)    │  │
│  │ per-blogger   │  │  RAG + ToV   │  │  profiles     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                  │          │
│  ┌──────┴─────────────────┴──────────────────┴───────┐  │
│  │              Shared Infrastructure                 │  │
│  │  PostgreSQL │ Redis │ ChromaDB                     │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │           ingestion-service (existing)            │   │
│  │  content pipeline: parse → transcribe → label →   │   │
│  │  vectorize → ChromaDB                             │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### White-label Config

Each blogger gets a YAML config in `config/bloggers/`:
- Bot token, names, welcome messages
- Tone of Voice system prompt
- ChromaDB collection name
- Branding (colors, avatar — for future Mini App)

The `BLOGGER_ID` env var selects which config a service instance uses.

### Data Flow: User asks a question

```
User (Telegram) → telegram-bot → llm-service /api/v1/ask
                                     │
                                     ├── embed query (OpenAI)
                                     ├── search ChromaDB (top-5 chunks)
                                     ├── build prompt (context + ToV + history)
                                     ├── call GPT-4o-mini
                                     └── return answer
                                          │
                        telegram-bot ←────┘
                             │
                        User (Telegram)
```

### Directory Structure

```
/workspace
├── config/
│   └── bloggers/
│       ├── yuri.yaml          # Yuri's bot config
│       └── maria.yaml         # Maria's bot config
├── libs/common/               # Shared models, config, DB (existing)
├── services/
│   ├── ingestion-service/     # Content pipeline (existing)
│   ├── telegram-bot/          # NEW — Telegram bot (aiogram)
│   ├── llm-service/           # NEW — RAG + LLM answering
│   └── user-service/          # NEW — User profiles + registration
├── migrations/                # Alembic (shared DB)
├── docker-compose.dev.yml     # All services
└── Makefile
```

## Stage 2 additions (planned)

- Diagnostics (static questionnaire + LLM follow-ups)
- Personal strategy generation
- "Situation analysis" chat mode with streaming
- Freemium + YuKassa payments
- Mini App (WebView) — basic screens
- AmoCRM integration
- Admin panel (basic)

## Stage 3 additions (planned)

- Full Mini App (all 9 screens)
- Progress tracking (balance wheel, streaks)
- Daily assignments (2 tracks)
- Long-term memory (hot sessions + cold JSON profile)
- Proactive pings microservice
- All payment providers
- Full admin panel (Metabase)
- Load testing (3000 concurrent users)
