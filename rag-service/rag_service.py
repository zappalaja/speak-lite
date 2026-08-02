#!/usr/bin/env python3
import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings


# --------- CONFIG (edit if needed) ----------
# Default path for containerized deployment; override with CHROMA_PERSIST_DIR env var
# For target system: CHROMA_PERSIST_DIR=/home/Jake.Zappala/SPEAR-RAG-Ingestion/chroma_db
PERSIST_DIR = os.environ.get(
    "CHROMA_PERSIST_DIR",
    "/app/chroma_db",
)

# MUST match what you used during ingestion
COLLECTION = os.environ.get("CHROMA_COLLECTION", "nougat_merged")

# MUST match what you used during ingestion
EMBED_MODEL = os.environ.get(
    "EMBED_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)

DEFAULT_K = int(os.environ.get("TOP_K", "5"))

# Path to the nougat_merged_md directory containing cleaned markdown + manifest
MERGED_MD_DIR = os.environ.get(
    "MERGED_MD_DIR",
    "/app/nougat_merged_md",
)

# Code Mode reference snippets (chatbot/code_snippets in the repo)
CODE_SNIPPETS_DIR = os.environ.get("CODE_SNIPPETS_DIR", "/app/code_snippets")
SNIPPETS_COLLECTION = os.environ.get("SNIPPETS_COLLECTION", "code_snippets")

# Ingestion pipeline paths
INGESTION_SCRIPTS_DIR = os.environ.get(
    "INGESTION_SCRIPTS_DIR",
    "/app/scripts",
)
INPUT_PDF_DIR = os.environ.get(
    "INPUT_PDF_DIR",
    "/app/pdfs",
)
# -------------------------------------------

# In-memory job tracker for ingestion pipelines
_ingestion_jobs: Dict[str, Dict[str, Any]] = {}


app = FastAPI(title="SPEAR RAG Service", version="0.1.0")

_embeddings = None
_vectordb = None


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(DEFAULT_K, ge=1, le=50)
    include_scores: bool = True


class RetrievedDoc(BaseModel):
    content: str
    metadata: Dict[str, Any]
    score: Optional[float] = None


class QueryResponse(BaseModel):
    query: str
    k: int
    results: List[RetrievedDoc]


def get_vectordb() -> Chroma:
    global _embeddings, _vectordb
    if _vectordb is None:
        if not os.path.isdir(PERSIST_DIR):
            raise RuntimeError(f"Chroma persist dir not found: {PERSIST_DIR}")

        _embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL)

        # This opens your existing persistent Chroma DB
        _vectordb = Chroma(
            collection_name=COLLECTION,
            persist_directory=PERSIST_DIR,
            embedding_function=_embeddings,
        )
    return _vectordb


@app.get("/health")
def health():
    try:
        db = get_vectordb()
        # lightweight call
        _ = db._collection.count()
        return {
            "ok": True,
            "persist_dir": PERSIST_DIR,
            "collection": COLLECTION,
            "embed_model": EMBED_MODEL,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    try:
        db = get_vectordb()

        if req.include_scores:
            docs_and_scores = db.similarity_search_with_score(req.query, k=req.k)
            results = [
                RetrievedDoc(content=d.page_content, metadata=d.metadata, score=float(s))
                for d, s in docs_and_scores
            ]
        else:
            docs = db.similarity_search(req.query, k=req.k)
            results = [RetrievedDoc(content=d.page_content, metadata=d.metadata) for d in docs]

        return QueryResponse(query=req.query, k=req.k, results=results)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- CODE MODE SNIPPETS ----------
# A second Chroma collection (same persist dir, same embedding model) indexing
# the developer-approved snippets in CODE_SNIPPETS_DIR. Each .py file's leading
# docstring is embedded for matching; the full code rides along as metadata.

_snippetdb = None
_snippets_indexed = False


def get_snippetdb() -> Chroma:
    global _embeddings, _snippetdb
    if _snippetdb is None:
        if _embeddings is None:
            _embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL)
        _snippetdb = Chroma(
            collection_name=SNIPPETS_COLLECTION,
            persist_directory=PERSIST_DIR,
            embedding_function=_embeddings,
        )
    return _snippetdb


def _snippet_description(code: str, fname: str) -> str:
    """The module docstring is what gets embedded for matching."""
    try:
        doc = ast.get_docstring(ast.parse(code))
    except SyntaxError:
        doc = None
    return doc.strip() if doc else fname


def reindex_snippets() -> int:
    """Rebuild the snippet collection from CODE_SNIPPETS_DIR."""
    global _snippets_indexed
    db = get_snippetdb()
    existing = db._collection.get()
    if existing["ids"]:
        db._collection.delete(ids=existing["ids"])

    texts, metas, ids = [], [], []
    if os.path.isdir(CODE_SNIPPETS_DIR):
        for fname in sorted(os.listdir(CODE_SNIPPETS_DIR)):
            if not fname.endswith(".py"):
                continue
            try:
                code = (Path(CODE_SNIPPETS_DIR) / fname).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not code:
                continue
            desc = _snippet_description(code, fname)
            texts.append(f"{fname}: {desc}")
            metas.append({"filename": fname, "description": desc, "code": code})
            ids.append(fname)
    if texts:
        db.add_texts(texts=texts, metadatas=metas, ids=ids)
    _snippets_indexed = True
    return len(texts)


class SnippetSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(2, ge=1, le=10)


@app.post("/snippets/reindex")
def snippets_reindex():
    """Re-scan the snippets folder — call after adding/editing snippet files."""
    try:
        n = reindex_snippets()
        return {"ok": True, "indexed": n, "dir": CODE_SNIPPETS_DIR}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/snippets/search")
def snippets_search(req: SnippetSearchRequest):
    try:
        if not _snippets_indexed:
            reindex_snippets()
        db = get_snippetdb()
        total = db._collection.count()
        if total == 0:
            return {"query": req.query, "results": []}
        docs_and_scores = db.similarity_search_with_score(req.query, k=min(req.k, total))
        return {
            "query": req.query,
            "results": [
                {
                    "filename": d.metadata.get("filename"),
                    "description": d.metadata.get("description"),
                    "code": d.metadata.get("code"),
                    "score": float(s),
                }
                for d, s in docs_and_scores
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- HELPER: load manifest ----------

def _load_manifest() -> List[Dict[str, Any]]:
    """Load manifest.json from MERGED_MD_DIR."""
    manifest_path = Path(MERGED_MD_DIR) / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"manifest.json not found at {manifest_path}")
    with open(manifest_path) as f:
        return json.load(f)


# ---------- DOCUMENT MODELS ----------

class DocumentInfo(BaseModel):
    title: str
    source_pdf: str
    chunk_count: int


class DocumentListResponse(BaseModel):
    documents: List[DocumentInfo]
    total_documents: int


class DocumentContentResponse(BaseModel):
    title: str
    source_pdf: str
    content: str


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)


class SearchResult(BaseModel):
    title: str
    match_count: int
    best_snippet: str


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]


# ---------- GET /documents ----------

@app.get("/documents", response_model=DocumentListResponse)
def list_documents():
    """List all unique documents in the ChromaDB collection."""
    try:
        db = get_vectordb()
        all_meta = db._collection.get(include=["metadatas"])
        metadatas = all_meta.get("metadatas", [])

        # Group by source file to get unique documents and chunk counts
        doc_map: Dict[str, Dict[str, Any]] = {}
        for meta in metadatas:
            source = meta.get("source", "unknown")
            if source not in doc_map:
                doc_map[source] = {
                    "title": meta.get("title", Path(source).stem),
                    "source_pdf": meta.get("title", Path(source).stem),
                    "chunk_count": 0,
                }
            doc_map[source]["chunk_count"] += 1

        documents = [
            DocumentInfo(**info) for info in sorted(doc_map.values(), key=lambda x: x["title"])
        ]
        return DocumentListResponse(documents=documents, total_documents=len(documents))

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- GET /manifest ----------

@app.get("/manifest")
def get_manifest():
    """Return the ingestion manifest (PDF→markdown mapping and provenance)."""
    return _load_manifest()


# ---------- GET /documents/{title}/content ----------

@app.get("/documents/{title}/content", response_model=DocumentContentResponse)
def get_document_content(title: str):
    """Return the cleaned markdown content for a document by title."""
    title = unquote(title)
    manifest = _load_manifest()

    # Find matching entry in manifest by PDF name (without extension)
    matched_entry = None
    for entry in manifest:
        pdf_name = entry.get("pdf", "")
        pdf_stem = Path(pdf_name).stem
        if pdf_stem == title or pdf_name == title:
            matched_entry = entry
            break

    # Fallback: partial match
    if matched_entry is None:
        for entry in manifest:
            pdf_name = entry.get("pdf", "")
            if title.lower() in pdf_name.lower():
                matched_entry = entry
                break

    if matched_entry is None:
        raise HTTPException(status_code=404, detail=f"Document not found: {title}")

    md_path = matched_entry.get("merged_md", "")
    if not md_path or not Path(md_path).exists():
        # Try resolving relative to MERGED_MD_DIR
        md_filename = Path(md_path).name if md_path else ""
        alt_path = Path(MERGED_MD_DIR) / md_filename
        if alt_path.exists():
            md_path = str(alt_path)
        else:
            raise HTTPException(status_code=404, detail=f"Markdown file not found: {md_path}")

    content = Path(md_path).read_text(encoding="utf-8")
    return DocumentContentResponse(
        title=Path(matched_entry["pdf"]).stem,
        source_pdf=matched_entry["pdf"],
        content=content,
    )


# ---------- POST /search ----------

@app.post("/search", response_model=SearchResponse)
def search_library(req: SearchRequest):
    """Keyword search across document titles and content from the manifest."""
    try:
        manifest = _load_manifest()
        keywords = req.query.lower().split()
        results = []

        for entry in manifest:
            if entry.get("status") != "ok":
                continue

            pdf_name = entry.get("pdf", "")
            title = Path(pdf_name).stem
            title_lower = title.lower()

            # Count keyword matches in title
            title_hits = sum(1 for kw in keywords if kw in title_lower)

            # Read markdown content and count keyword matches there
            content_hits = 0
            snippet = ""
            md_path = entry.get("merged_md", "")
            if md_path and Path(md_path).exists():
                content = Path(md_path).read_text(encoding="utf-8")
                content_lower = content.lower()
                content_hits = sum(content_lower.count(kw) for kw in keywords)

                # Extract a snippet around the first keyword match
                for kw in keywords:
                    idx = content_lower.find(kw)
                    if idx >= 0:
                        start = max(0, idx - 100)
                        end = min(len(content), idx + 200)
                        snippet = content[start:end].strip()
                        break

            total_hits = title_hits * 10 + content_hits  # weight title matches higher
            if total_hits > 0:
                results.append({
                    "title": title,
                    "match_count": total_hits,
                    "best_snippet": snippet if snippet else title[:300],
                })

        # Sort by match count descending, limit to k
        results.sort(key=lambda x: x["match_count"], reverse=True)
        results = results[:req.k]

        return SearchResponse(
            query=req.query,
            results=[SearchResult(**r) for r in results],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# INGESTION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def _write_conf(path: Path, values: Dict[str, str]):
    """Write a shell-sourceable config file."""
    with open(path, "w") as f:
        for k, v in values.items():
            f.write(f'{k}="{v}"\n')


def _promote_results(staging_dir: Path):
    """
    After successful ingestion, copy new files into the main directories
    and merge the staging manifest into the main manifest.
    """
    staging_pdfs = staging_dir / "pdfs"
    staging_merged = staging_dir / "merged_md"
    main_pdf_dir = Path(INPUT_PDF_DIR)
    main_merged_dir = Path(MERGED_MD_DIR)

    main_pdf_dir.mkdir(parents=True, exist_ok=True)
    main_merged_dir.mkdir(parents=True, exist_ok=True)

    # Copy PDFs to main directory
    for pdf in staging_pdfs.glob("*.pdf"):
        shutil.copy2(pdf, main_pdf_dir / pdf.name)

    # Copy merged markdown files to main directory
    for md in staging_merged.glob("*.md"):
        shutil.copy2(md, main_merged_dir / md.name)

    # Merge manifests
    staging_manifest_path = staging_merged / "manifest.json"
    main_manifest_path = main_merged_dir / "manifest.json"

    staging_manifest = []
    if staging_manifest_path.exists():
        staging_manifest = json.loads(staging_manifest_path.read_text(encoding="utf-8"))

    main_manifest = []
    if main_manifest_path.exists():
        main_manifest = json.loads(main_manifest_path.read_text(encoding="utf-8"))

    # Build set of existing PDFs by sha256 to avoid duplicates
    existing_hashes = {e.get("pdf_sha256") for e in main_manifest if e.get("pdf_sha256")}

    for entry in staging_manifest:
        if entry.get("pdf_sha256") in existing_hashes:
            continue
        # Update merged_md path to point to the main directory
        if entry.get("merged_md"):
            md_name = Path(entry["merged_md"]).name
            entry["merged_md"] = str(main_merged_dir / md_name)
        main_manifest.append(entry)

    main_manifest_path.write_text(json.dumps(main_manifest, indent=2), encoding="utf-8")

    # Merge pdf_sha256.json
    staging_hash_path = staging_merged / "pdf_sha256.json"
    main_hash_path = main_merged_dir / "pdf_sha256.json"

    main_hashes = {}
    if main_hash_path.exists():
        main_hashes = json.loads(main_hash_path.read_text(encoding="utf-8"))
    if staging_hash_path.exists():
        staging_hashes = json.loads(staging_hash_path.read_text(encoding="utf-8"))
        main_hashes.update(staging_hashes)
    main_hash_path.write_text(json.dumps(main_hashes, indent=2), encoding="utf-8")


def _run_ingestion(job_id: str, staging_dir: Path):
    """
    Background thread: runs the two-stage ingestion pipeline, updates job status,
    and promotes results to main directories on success.
    """
    global _vectordb
    job = _ingestion_jobs[job_id]
    scripts = Path(INGESTION_SCRIPTS_DIR)
    log_lines = []

    try:
        # ── Stage 1: Nougat OCR ──
        job["status"] = "nougat_running"
        nougat_conf = staging_dir / "nougat_stage.conf"
        _write_conf(nougat_conf, {
            "INPUT_PDF_DIR": str(staging_dir / "pdfs"),
            "NOUGAT_OUT_DIR": str(staging_dir / "nougat_out"),
            "MERGED_MD_DIR": str(staging_dir / "merged_md"),
            "LOG_DIR": str(staging_dir / "logs"),
            "CONDA_ENV": os.environ.get("NOUGAT_CONDA_ENV", "nougat"),
            "NOUCAT_EXTRA_ARGS": "--markdown",
        })

        nougat_script = scripts / "nougat_stage.sh"
        proc = subprocess.run(
            ["bash", str(nougat_script), str(nougat_conf)],
            capture_output=True, text=True, timeout=3600,  # 1 hour max
        )
        log_lines.append("=== NOUGAT STAGE ===")
        log_lines.append(proc.stdout)
        if proc.stderr:
            log_lines.append(proc.stderr)
        job["log"] = "\n".join(log_lines)

        if proc.returncode != 0:
            job["status"] = "failed"
            return

        # ── Stage 2: RAG Embedding ──
        job["status"] = "rag_running"
        rag_conf = staging_dir / "rag_stage.conf"
        _write_conf(rag_conf, {
            "MERGED_MD_DIR": str(staging_dir / "merged_md"),
            "CHROMA_DIR": PERSIST_DIR,
            "COLLECTION": COLLECTION,
            "EMBEDDING_MODEL": EMBED_MODEL,
            "CHUNK_SIZE": "1200",
            "CHUNK_OVERLAP": "150",
            "CONDA_ENV": os.environ.get("CONDA_ENV", "spear"),
        })

        rag_script = scripts / "rag_stage.sh"
        proc = subprocess.run(
            ["bash", str(rag_script), str(rag_conf)],
            capture_output=True, text=True, timeout=1800,  # 30 min max
        )
        log_lines.append("\n=== RAG STAGE ===")
        log_lines.append(proc.stdout)
        if proc.stderr:
            log_lines.append(proc.stderr)
        job["log"] = "\n".join(log_lines)

        if proc.returncode != 0:
            job["status"] = "failed"
            return

        # ── Promote results to main directories ──
        _promote_results(staging_dir)

        # Invalidate cached vectordb so next query picks up new data
        _vectordb = None

        job["status"] = "completed"

    except subprocess.TimeoutExpired:
        log_lines.append("\nERROR: Pipeline timed out.")
        job["log"] = "\n".join(log_lines)
        job["status"] = "failed"
    except Exception as e:
        log_lines.append(f"\nERROR: {e}")
        job["log"] = "\n".join(log_lines)
        job["status"] = "failed"


# ---------- POST /ingest ----------

MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", "100"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@app.post("/ingest")
def ingest(files: List[UploadFile] = File(...)):
    """Upload PDF files and trigger the Nougat → RAG ingestion pipeline."""
    # Validate files
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"Only PDF files accepted: {f.filename}")

    # Verify ingestion scripts exist
    scripts = Path(INGESTION_SCRIPTS_DIR)
    if not (scripts / "nougat_stage.sh").exists():
        raise HTTPException(
            status_code=500,
            detail=f"nougat_stage.sh not found in {INGESTION_SCRIPTS_DIR}",
        )
    if not (scripts / "rag_stage.sh").exists():
        raise HTTPException(
            status_code=500,
            detail=f"rag_stage.sh not found in {INGESTION_SCRIPTS_DIR}",
        )

    # Load existing document titles/hashes for duplicate detection
    existing_hashes: set = set()
    existing_titles: set = set()

    # Check 1: manifest file (SHA256 hashes)
    manifest_path = Path(MERGED_MD_DIR) / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_hashes = {e.get("pdf_sha256") for e in manifest if e.get("pdf_sha256")}
        except Exception:
            pass

    # Check 2: ChromaDB metadata (document titles) — works even if manifest is missing
    try:
        db = get_vectordb()
        all_meta = db._collection.get(include=["metadatas"])
        for meta in all_meta.get("metadatas", []):
            title = meta.get("title", "")
            if title:
                existing_titles.add(title.lower())
    except Exception:
        pass

    # Create staging directory
    job_id = str(uuid.uuid4())
    staging_dir = Path(tempfile.mkdtemp(prefix=f"rag_ingest_{job_id[:8]}_"))
    (staging_dir / "pdfs").mkdir()
    (staging_dir / "nougat_out").mkdir()
    (staging_dir / "merged_md").mkdir()
    (staging_dir / "logs").mkdir()

    # Save uploaded PDFs to staging with size + duplicate checks
    filenames = []
    skipped = []
    max_bytes = MAX_PDF_SIZE_MB * 1024 * 1024

    for f in files:
        data = f.file.read()

        # File size check
        if len(data) > max_bytes:
            size_mb = len(data) / (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=f"{f.filename} is {size_mb:.1f} MB, exceeds limit of {MAX_PDF_SIZE_MB} MB.",
            )

        # Duplicate check via SHA256 hash or filename match in ChromaDB
        file_hash = _sha256_bytes(data)
        file_title = Path(f.filename).stem.lower()
        if file_hash in existing_hashes or file_title in existing_titles:
            skipped.append(f.filename)
            continue

        dest = staging_dir / "pdfs" / f.filename
        dest.write_bytes(data)
        filenames.append(f.filename)

    if not filenames:
        # All files were duplicates
        return {
            "job_id": None,
            "status": "skipped",
            "files": [],
            "skipped_duplicates": skipped,
            "message": "All uploaded files already exist in the database.",
        }

    # Register job
    _ingestion_jobs[job_id] = {
        "job_id": job_id,
        "status": "started",
        "files": filenames,
        "skipped_duplicates": skipped,
        "started_at": time.time(),
        "staging_dir": str(staging_dir),
        "log": "",
    }

    # Launch pipeline in background thread
    thread = threading.Thread(target=_run_ingestion, args=(job_id, staging_dir), daemon=True)
    thread.start()

    result = {"job_id": job_id, "status": "started", "files": filenames}
    if skipped:
        result["skipped_duplicates"] = skipped
    return result


# ---------- GET /ingest/{job_id}/status ----------

@app.get("/ingest/{job_id}/status")
def ingest_status(job_id: str):
    """Poll the status of an ingestion job."""
    if job_id not in _ingestion_jobs:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    job = _ingestion_jobs[job_id]
    elapsed = time.time() - job["started_at"]

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "files": job["files"],
        "elapsed_seconds": round(elapsed, 1),
        "log": job["log"],
    }


# ---------- DELETE /documents/{title} ----------

@app.delete("/documents/{title}")
def delete_document(title: str):
    """Delete a document from ChromaDB and the manifest."""
    global _vectordb
    title = unquote(title)

    # ── Remove chunks from ChromaDB ──
    try:
        db = get_vectordb()
        all_data = db._collection.get(include=["metadatas"])
        ids_to_delete = []
        for doc_id, meta in zip(all_data["ids"], all_data["metadatas"]):
            doc_title = meta.get("title", "")
            if doc_title == title or doc_title.lower() == title.lower():
                ids_to_delete.append(doc_id)

        if not ids_to_delete:
            raise HTTPException(status_code=404, detail=f"No chunks found for document: {title}")

        db._collection.delete(ids=ids_to_delete)
        chunks_deleted = len(ids_to_delete)

        # Invalidate cache so next query reflects deletion
        _vectordb = None

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete from ChromaDB: {e}")

    # ── Remove from manifest ──
    manifest_path = Path(MERGED_MD_DIR) / "manifest.json"
    manifest_removed = False
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original_len = len(manifest)
            manifest = [
                e for e in manifest
                if Path(e.get("pdf", "")).stem.lower() != title.lower()
            ]
            if len(manifest) < original_len:
                manifest_removed = True
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "deleted": True,
        "title": title,
        "chunks_removed": chunks_deleted,
        "manifest_updated": manifest_removed,
    }
