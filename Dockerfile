FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/
COPY dashboard/ dashboard/
COPY sample_data/ sample_data/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    LLM_BACKEND=ollama \
    DATA_DIR=/app/data/local

# Non-root: the agent reads config/ and sample_data/ and writes only under DATA_DIR.
RUN mkdir -p /app/data/local /app/logs \
 && useradd --uid 10001 memo && chown -R memo:memo /app
USER memo

EXPOSE 8000 8501

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
