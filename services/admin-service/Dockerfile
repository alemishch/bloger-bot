FROM python:3.11-slim

WORKDIR /app

COPY services/admin-service/pyproject.toml services/admin-service/
RUN pip install --no-cache-dir ./services/admin-service

COPY services/admin-service/src/ src/

ENV PYTHONPATH=/app/src
EXPOSE 8000

CMD ["uvicorn", "admin_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
