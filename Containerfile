# SPEAR windy viewer — podman/docker compatible.
#   podman build -t spear-windy .
#   podman run --rm -p 8601:8601 --env-file .env spear-windy
FROM python:3.12-slim

WORKDIR /app

# CPU-only torch first (sentence-transformers would otherwise pull the
# multi-GB CUDA build), then everything else.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so first start doesn't download.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY windy_viewer/ windy_viewer/
COPY rag-service/ rag-service/

ENV VIEWER_HOST=0.0.0.0 \
    VIEWER_PORT=8601 \
    VIEWER_CACHE_MAX_GB=10

EXPOSE 8601

# server.py auto-starts the RAG service (port 8002, internal) alongside it
CMD ["python", "windy_viewer/server.py"]
