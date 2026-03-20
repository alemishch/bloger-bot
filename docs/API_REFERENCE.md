# API Reference — Bloger Bot

Документ для фронтенд-разработчика (Mini App) и DevOps-инженера.

---

## Архитектура сервисов

| Сервис | Порт | Назначение | Swagger UI |
|--------|------|------------|------------|
| **ingestion-service** | 8002 | Пайплайн контента (парсинг, транскрибация, разметка, векторизация) | `http://host:8002/docs` |
| **llm-service** | 8003 | RAG-движок + анализ профиля + обновление памяти | `http://host:8003/docs` |
| **user-service** | 8004 | Пользователи, онбординг, сессии, сообщения | `http://host:8004/docs` |
| **admin-service** | 8010 | Админ-панель, аналитика, экспорт контента | `http://host:8010/docs` |
| **telegram-bot-yuri** | — | Telegram-бот (aiogram 3.x, polling) | нет UI |
| postgres | 5432 | PostgreSQL 16 | — |
| redis | 6379 | Redis 7 (брокер Celery) | — |
| chromadb | 8000 | Векторная БД ChromaDB 0.5.23 | — |

---

## User Service (порт 8004)

Основной API для Mini App. Все данные о пользователях, онбординге, сессиях и сообщениях.

### GET `/api/v1/users/{telegram_id}`

Получить профиль пользователя.

**Ответ:**
```json
{
  "id": "uuid",
  "telegram_id": 437643366,
  "blogger_id": "yuri",
  "username": "kistup",
  "first_name": "Анна",
  "last_name": null,
  "phone": null,
  "email": null,
  "onboarding_status": "completed",
  "onboarding_step": "complete",
  "profile_data": null,
  "long_term_profile": {
    "name": "Анна",
    "goals": "разобраться с усталостью",
    "topics_of_interest": ["усталость", "ЖКТ", "тревога"],
    "last_session_summary": "Обсуждали повторяющиеся боли..."
  },
  "created_at": "2026-03-16T12:04:49"
}
```

### GET `/api/v1/users/{telegram_id}/onboarding`

Ответы пользователя из онбординга.

**Ответ:**
```json
[
  {"step_id": "symptoms", "answer_value": "fatigue,gut,anxiety", "created_at": "..."},
  {"step_id": "duration", "answer_value": "over_year", "created_at": "..."},
  {"step_id": "tried", "answer_value": "doctors,meds", "created_at": "..."},
  {"step_id": "lifestyle", "answer_value": "busy_family", "created_at": "..."},
  {"step_id": "blocker", "answer_value": "no_motivation", "created_at": "..."},
  {"step_id": "repeating", "answer_value": "yes_pattern", "created_at": "..."},
  {"step_id": "expert_experience", "answer_value": "new", "created_at": "..."}
]
```

### GET `/api/v1/users/{telegram_id}/sessions`

Список чат-сессий пользователя.

**Параметры:** `limit` (int, default 10, max 50)

**Ответ:**
```json
[
  {
    "id": "session-uuid",
    "is_active": true,
    "started_at": "2026-03-16T15:06:40",
    "last_message_at": "2026-03-16T15:10:20",
    "message_count": 8
  }
]
```

### GET `/api/v1/sessions/{session_id}/messages`

История сообщений конкретной сессии.

**Ответ:**
```json
[
  {"id": "uuid", "role": "user", "content": "Что делать с усталостью?", "token_count": null, "created_at": "..."},
  {"id": "uuid", "role": "assistant", "content": "Давайте посмотрим...", "token_count": 150, "created_at": "..."}
]
```

---

## LLM Service (порт 8003)

RAG-движок для ответов на вопросы и анализа профиля.

### POST `/api/v1/ask`

Задать вопрос — получить ответ из базы знаний через RAG.

**Тело запроса:**
```json
{
  "query": "в чём причина моей усталости?",
  "blogger_id": "yuri",
  "chat_history": [
    {"role": "user", "content": "предыдущий вопрос"},
    {"role": "assistant", "content": "предыдущий ответ"}
  ],
  "user_profile": {
    "name": "Анна",
    "goals": "разобраться с усталостью",
    "topics_of_interest": ["усталость", "ЖКТ"]
  }
}
```

**Ответ:**
```json
{
  "answer": "Давайте посмотрим на вашу ситуацию иначе...",
  "sources": [
    {"chunk": "фрагмент из базы знаний...", "similarity": 0.82}
  ],
  "usage": {"prompt_tokens": 900, "completion_tokens": 200}
}
```

### POST `/api/v1/analyze`

Анализ онбординга → проблемные зоны + гипотезы.

**Тело запроса:**
```json
{
  "onboarding_responses": [
    {"step_id": "symptoms", "answer_value": "fatigue,gut"},
    {"step_id": "duration", "answer_value": "over_year"}
  ],
  "blogger_id": "yuri",
  "user_name": "Анна"
}
```

**Ответ:**
```json
{
  "analysis": "Я внимательно изучил ваши ответы...",
  "usage": {"prompt_tokens": 1000, "completion_tokens": 300}
}
```

### POST `/api/v1/update-profile`

Агент-апдейтер: анализ диалога → обновление long-term профиля (§14.3).

**Тело запроса:**
```json
{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "current_profile": {"name": "Анна", "goals": "..."},
  "blogger_id": "yuri",
  "user_name": "Анна"
}
```

**Ответ:**
```json
{
  "profile": {
    "name": "Анна",
    "communication_style": "формально, предпочитает развёрнутые ответы",
    "goals": "разобраться с хронической усталостью",
    "topics_of_interest": ["усталость", "ЖКТ", "тревога"],
    "reactions": {},
    "last_session_summary": "Обсуждали связь усталости...",
    "previous_session_summary": "..."
  },
  "summary": "Обсуждали связь усталости...",
  "usage": {"prompt_tokens": 800, "completion_tokens": 200}
}
```

---

## Admin Service (порт 8010)

Админ-панель с дашбордом и API для аналитики.

### HTML-страницы

| URL | Описание |
|-----|----------|
| `/` | Дашборд: пользователи, онбординг, пайплайн |
| `/users` | Список пользователей с фильтрами |
| `/dialogues/{telegram_id}` | Диалоги пользователя |

### REST API

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/v1/stats/overview` | Общая статистика (DAU, онбординг, сообщения) |
| GET | `/api/v1/stats/pipeline` | Пайплайн контента по статусам |
| GET | `/api/v1/stats/onboarding` | Аналитика онбординга (воронка, симптомы) |
| GET | `/api/v1/stats/activity?days=30` | Активность по дням (DAU, сообщения) |
| GET | `/api/v1/users?limit=50&offset=0&status=completed` | Список пользователей |
| GET | `/api/v1/users/{telegram_id}/dialogues` | Список сессий пользователя |
| GET | `/api/v1/dialogues/{session_id}` | Сообщения сессии |
| GET | `/api/v1/export/content?status=ready` | CSV-экспорт контента для верификации |

### Prometheus метрики

`GET /metrics` — стандартные метрики Prometheus для Grafana.

---

## Ingestion Service (порт 8002)

Управление контентом: парсинг, обработка, пайплайн.

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/v1/sources/` | Список источников контента |
| POST | `/api/v1/sources/` | Создать источник |
| POST | `/api/v1/sources/{id}/parse` | Запустить парсинг (видео + текст) |
| POST | `/api/v1/sources/{id}/parse-text` | Парсинг только текстовых постов (фоновый) |
| POST | `/api/v1/sources/cancel-task/{task_id}` | Отменить фоновую задачу |
| GET | `/api/v1/jobs/stats` | Статистика пайплайна |
| GET | `/api/v1/jobs/` | Список элементов контента |
| POST | `/api/v1/jobs/recover-all` | Восстановить застрявшие/упавшие задачи |

---

## База данных (PostgreSQL)

### Таблицы

| Таблица | Назначение |
|---------|-----------|
| `users` | Пользователи (telegram_id, blogger_id, onboarding, profile) |
| `chat_sessions` | Чат-сессии (2ч таймаут, summary) |
| `chat_messages` | Сообщения (role, content, token_count) |
| `onboarding_responses` | Ответы онбординга (step_id, answer_value) |
| `content_sources` | Источники контента (Telegram каналы/чаты) |
| `content_items` | Элементы контента (видео, посты, статус пайплайна) |
| `content_chunks` | Чанки для векторизации |

### Ключевые поля users

```
telegram_id       BIGINT UNIQUE
blogger_id        ENUM (yuri, maria)
onboarding_status ENUM (not_started, in_progress, completed)
onboarding_step   VARCHAR(64)
long_term_profile JSON — долгосрочный профиль пользователя (§14.4)
profile_data      JSON — дополнительные данные
```

### Структура long_term_profile (§14.4)

```json
{
  "name": "как пользователь просит себя называть",
  "communication_style": "формально/неформально, короткие/развёрнутые",
  "goals": "чего хочет достичь",
  "topics_of_interest": ["усталость", "ЖКТ", "тревога"],
  "reactions": {"positive": [...], "negative": [...]},
  "last_session_summary": "3-5 предложений",
  "previous_session_summary": "3-5 предложений"
}
```
Ограничение: ≤ 4000 символов. Обновляется агентом-апдейтером после закрытия сессии.

---

## Конфигурация (white-label)

### Структура конфигов

```
config/
├── bloggers/
│   ├── yuri.yaml    — токен, Tone of Voice, RAG-параметры, роли, зоны
│   └── maria.yaml   — аналогично для Марии
└── onboarding/
    └── yuri.yaml    — сценарий онбординга (шаги, вопросы, лид-магниты)
```

### Переменные окружения

| Переменная | Описание |
|-----------|----------|
| `BLOGGER_ID` | Выбор конфига блогера (yuri / maria) |
| `TELEGRAM_BOT_TOKEN_YURI` | Токен бота из @BotFather |
| `OPENAI_API_KEY` | Ключ OpenAI для LLM + embeddings |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | PostgreSQL |
| `REDIS_PASSWORD` | Пароль Redis |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Pyrogram (парсинг каналов) |

---

## Docker Compose

Запуск всех сервисов: `docker compose -f docker-compose.dev.yml up -d --build`

### Сервисы для Mini App разработчика

Фронтенд Mini App будет обращаться к:
1. **user-service:8004** — профили, онбординг, сессии, сообщения
2. **llm-service:8003** — отправка вопросов, получение ответов
3. Аутентификация через Telegram WebApp API (initData → telegram_id)

### Мониторинг (для DevOps)

- Prometheus: `admin-service:8000/metrics`
- Grafana: порт 3000 (профиль `monitoring` в docker-compose)
- Healthchecks: `GET /health` на каждом сервисе
- Логи: `docker compose logs -f <service-name>`

### Добавление нового блогера

1. Создать `config/bloggers/<id>.yaml` и `config/onboarding/<id>.yaml`
2. Добавить сервис `telegram-bot-<id>` в docker-compose (копия yuri, другой `BLOGGER_ID`)
3. Заполнить базу контента через ingestion-service
4. Создать коллекцию в ChromaDB: `blogger_<id>`
