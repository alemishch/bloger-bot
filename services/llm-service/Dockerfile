FROM python:3.11-slim

WORKDIR /app

COPY services/llm-service/pyproject.toml services/llm-service/
RUN pip install --no-cache-dir ./services/llm-service

COPY services/llm-service/src/ src/
COPY config/ config/

ENV PYTHONPATH=/app/src
EXPOSE 8000

CMD ["uvicorn", "llm_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
