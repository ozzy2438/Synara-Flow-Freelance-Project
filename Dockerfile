FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY README.md /app/README.md

RUN pip install --no-cache-dir .

EXPOSE 8000 8501
CMD ["uvicorn", "synara.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
