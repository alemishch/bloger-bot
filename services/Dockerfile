FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install common lib first
COPY libs/common/pyproject.toml libs/common/
COPY libs/common/src/ libs/common/src/
RUN pip install --no-cache-dir ./libs/common

# Install service
COPY services/ingestion-service/pyproject.toml .
RUN pip install --no-cache-dir .

COPY services/ingestion-service/src/ src/

ENV PYTHONPATH=/app/src
EXPOSE 8000

CMD ["uvicorn", "ingestion_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]