FROM python:3.11-slim

WORKDIR /app

COPY services/user-service/pyproject.toml services/user-service/
RUN pip install --no-cache-dir ./services/user-service

COPY services/user-service/src/ src/

ENV PYTHONPATH=/app/src
EXPOSE 8000

CMD ["uvicorn", "user_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
