FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/common/pyproject.toml libs/common/
COPY libs/common/src/ libs/common/src/
RUN pip install --no-cache-dir ./libs/common

COPY services/ingestion-service/pyproject.toml services/ingestion-service/
RUN pip install --no-cache-dir ./services/ingestion-service

RUN pip install --no-cache-dir alembic

COPY services/ingestion-service/src/ src/
COPY migrations/ migrations/
RUN mkdir -p migrations/versions

ENV PYTHONPATH=/app/src:/app/libs/common/src
EXPOSE 8000

CMD ["uvicorn", "ingestion_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]