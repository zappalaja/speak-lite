# SPEAK-Lite Visualization

An interactive viewer for the **SPEAR-MED large
ensemble**, streaming data on demand from the public ArrayLake/Icechunk store
(`GFDL/noaa-gfdl-spear-large-ensembles-pds`).

**Features**: 16 monthly variables (atmosphere incl. pressure levels, ocean
SST) · ensemble member / mean / spread / anomaly statistics · A−B compare
mode · wind particle animation · rotatable 3-D globe view · virtual stations
with time-series extraction and merged plots · year playback · value
filtering · unit conversion · NetCDF/CSV downloads with full source
metadata · toggleable graticule · **SPEAK**, a rate-limited RAG chatbot
(Gemini) that sees the on-screen selection statistics.

## Layout

```
windy_viewer/    FastAPI server (port 8601) + static Leaflet frontend
rag-service/     document-retrieval API for SPEAK (port 8002, internal)
                 chroma_db/ = pre-built vector index of GFDL/SPEAR papers
requirements.txt one environment for both services
Containerfile    podman/docker build
```

The viewer auto-starts the RAG service beside itself. Without the RAG
service (or without an LLM key) everything still works. SPEAK will just
stay hidden.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate   # or conda
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
cp .env.example .env                            # fill in tokens
python windy_viewer/server.py                   # http://localhost:8601
```

## Run with podman

```bash
podman build -t spear-windy .
podman run --rm -p 8601:8601 --env-file .env spear-windy
```

Optional: persist the LRU field cache across restarts

```bash
podman run --rm -p 8601:8601 --env-file .env \
  -v spear-windy-cache:/app/windy_viewer/data spear-windy
```

## Deploy notes (AWS or similar)

- Needs outbound HTTPS to ArrayLake/S3 (data), Esri/CARTO (basemap tiles are
  fetched by the *browser*, not the server), and the LLM APIs.
- `VIEWER_HOST=0.0.0.0` is set in the image; put a TLS proxy (ALB, nginx,
  Caddy) in front for public exposure.
- `VIEWER_DISK_CACHE=0` for fully stateless replicas, or mount a volume and
  set `VIEWER_CACHE_MAX_GB` (default 10) — the cache is selection-keyed and
  safe to share.
- Chat abuse limits are on by default (per-IP and global daily caps); tune
  via the `CHAT_*` vars in `.env.example`.
- RAM: ~2 GB is comfortable (embedding model + field buffers).

## Keys/Secrets

`ARRAYLAKE_TOKEN` (required), `GEMINI_API_KEY`
(optional, enables SPEAK), `CARTO_API_KEY` (optional; without it CARTO
watermarks the basemap tiles. Get a free key at
https://carto.com/basemaps/apikey/;
the key is served to the browser via `/config.js`). Loaded from `.env` at the repo root (which is
git-ignored); only `.env.example` is committed.
