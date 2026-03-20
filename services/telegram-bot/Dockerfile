FROM python:3.11-slim

WORKDIR /app

COPY services/telegram-bot/pyproject.toml services/telegram-bot/
RUN pip install --no-cache-dir ./services/telegram-bot

COPY services/telegram-bot/src/ src/
COPY config/ config/

ENV PYTHONPATH=/app/src
CMD ["python", "-m", "bot.main"]
