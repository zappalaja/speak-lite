"""Local Windy-style viewer for gridded monthly climate data.

Serves a Leaflet frontend plus a small JSON API that pulls single
(dataset, variable, experiment, member, month) fields from the configured
data sources — ArrayLake repos and/or local NetCDF collections catalogued
by tools/nc_catalog.py (see sources.py, datasets.example.json) — and
caches them on disk.

Run:  python server.py   (inside the `spear` conda env)
Then open http://localhost:8601
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time as time_mod
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

import numpy as np
import xarray as xr
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent

# Writable scratch (field cache, RAG log): VIEWER_DATA_DIR, else
# windy_viewer/data in the checkout. If that can't be created (read-only
# or full home directory on a shared system), fall back to a temp dir so
# the viewer still starts — the cache is only an accelerator.
DATA_DIR = Path(os.environ.get("VIEWER_DATA_DIR") or BASE_DIR / "data")
try:
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)
except OSError as _e:
    _fallback = Path(tempfile.gettempdir()) / f"windy-viewer-{os.getuid()}"
    print(f"[viewer] cannot use {DATA_DIR} ({_e.strerror}); using {_fallback} — set VIEWER_DATA_DIR")
    DATA_DIR = _fallback
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"

# Optional response cache. Every selection change always maps to a fresh
# ArrayLake query for exactly that slice; the cache only skips re-querying
# identical selections. Set VIEWER_DISK_CACHE=0 to run fully stateless
# (e.g. cloud hosting where nothing should persist locally).
# The cache is shared by all users (entries are keyed by selection only)
# and the selection space is ~TBs, so it is LRU-bounded: when it exceeds
# VIEWER_CACHE_MAX_GB (default 10), least-recently-used files are evicted
# down to 90% of the cap.
CACHE_ENABLED = os.environ.get("VIEWER_DISK_CACHE", "1").lower() not in ("0", "false")
CACHE_MAX_BYTES = int(float(os.environ.get("VIEWER_CACHE_MAX_GB", "10")) * 1e9)
_cache_lock = threading.Lock()


def _cache_get(path: Path):
    if CACHE_ENABLED and path.exists():
        try:
            os.utime(path)  # mark as recently used for LRU eviction
        except OSError:
            pass
        return Response(path.read_bytes(), media_type="application/json")
    return None


def _evict_cache():
    entries = []
    total = 0
    for p in CACHE_DIR.iterdir():
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    if total <= CACHE_MAX_BYTES:
        return
    entries.sort()  # oldest access first
    target = int(CACHE_MAX_BYTES * 0.9)
    for _, size, p in entries:
        if total <= target:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass


def _cache_put(path: Path, body: bytes):
    if CACHE_ENABLED:
        path.write_bytes(body)
        with _cache_lock:
            _evict_cache()

# Credentials: ARRAYLAKE_TOKEN (+ GEMINI_API_KEY etc.) from the repo-root
# .env, falling back to ~/.arraylake/token.json from `arraylake auth login`.
load_dotenv(BASE_DIR.parent / ".env")

from sources import load_sources  # noqa: E402  (after load_dotenv: ArrayLake token)

# Configured data sources (datasets.json / VIEWER_DATASETS, else the
# built-in SPEAR definition). Each opens lazily on first use; see sources.py.
SOURCES = load_sources()
DEFAULT_SOURCE = next(iter(SOURCES))
STATS = ("raw", "mean", "spread", "anom")


def _src(dataset: str | None):
    key = dataset or DEFAULT_SOURCE
    s = SOURCES.get(key)
    if s is None:
        raise HTTPException(400, f"unknown dataset {key!r}")
    try:
        return s.ensure()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"dataset {key!r} unavailable: {str(e)[:200]}")


def _open(src, experiment: str, var: str) -> xr.Dataset:
    """src.open() with unknown names mapped to HTTP 400."""
    if var not in src.variables:
        raise HTTPException(400, f"unknown variable {var!r}")
    if experiment not in src.experiments:
        raise HTTPException(400, f"unknown experiment {experiment!r}")
    return src.open(experiment, var)


app = FastAPI(title="windy viewer")


@app.middleware("http")
async def no_stale_static(request, call_next):
    """Make browsers revalidate the frontend files on every load, so code
    updates apply on a plain reload instead of requiring a hard refresh."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------- RAG / chat
RAG_API_URL = os.environ.get("RAG_API_URL", "http://localhost:8002")
RAG_DIR = BASE_DIR.parent / "rag-service"
LLM_PROVIDER = os.environ.get("VIEWER_CHAT_PROVIDER", "gemini")
# gemini-flash-latest is Google's rolling alias for their newest flash
# model, so the chat always uses the newest available by default.
LLM_MODEL = os.environ.get("VIEWER_CHAT_MODEL", "gemini-flash-latest")

_rag_proc = None


def _ensure_rag_service():
    """Start the project's RAG service alongside the viewer if it isn't
    already running (mirrors start_unified.sh's launch environment)."""
    global _rag_proc
    try:
        r = requests.get(f"{RAG_API_URL}/health", timeout=2)
        if r.ok:
            return
    except Exception:
        pass
    if not RAG_DIR.is_dir():
        return
    env = os.environ.copy()
    env.setdefault("CHROMA_PERSIST_DIR", str(RAG_DIR / "chroma_db"))
    env.setdefault("CHROMA_COLLECTION", "nougat_merged")
    env.setdefault("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    env.setdefault("CODE_SNIPPETS_DIR", str(RAG_DIR / "code_snippets"))
    log = open(DATA_DIR / "rag.log", "ab")
    _rag_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rag_service:app",
         "--host", "127.0.0.1", "--port", RAG_API_URL.rsplit(":", 1)[-1]],
        cwd=RAG_DIR, env=env, stdout=log, stderr=log,
    )


@app.on_event("startup")
def _warm_datasets():
    """Open the experiment groups in the background so the first switch
    doesn't stall, and bring up the RAG service alongside the viewer."""

    def warm():
        _ensure_rag_service()
        for s in SOURCES.values():
            try:
                s.ensure()
            except Exception as e:  # noqa: BLE001
                print(f"[viewer] dataset {s.id!r} failed to open: {e}")

    threading.Thread(target=warm, daemon=True).start()


# CARTO basemap API key (https://carto.com/basemaps/apikey/). Tiles are
# fetched by the browser, so the key is necessarily visible client-side;
# it still lives in .env (not source) and is restricted by CARTO to the
# domains registered for it. Without a key CARTO watermarks every tile.
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "").strip()
if not CARTO_API_KEY:
    print("[viewer] CARTO_API_KEY not set — basemap tiles will carry a watermark")


@app.get("/config.js")
def config_js():
    """Runtime config for the frontend, loaded before app.js."""
    body = "window.SPEAR_CONFIG = " + json.dumps({"cartoApiKey": CARTO_API_KEY}) + ";\n"
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/meta")
def meta():
    datasets = []
    for src in SOURCES.values():
        try:
            datasets.append(src.meta())
        except Exception as e:  # noqa: BLE001
            datasets.append({"id": src.id, "title": src.title, "error": str(e)[:200]})
    first = next((d for d in datasets if "error" not in d), None)
    if first is None:
        raise HTTPException(503, "no dataset is reachable: "
                            + "; ".join(f"{d['id']}: {d.get('error', '')}" for d in datasets))
    return {
        "datasets": datasets,
        "default": first["id"],
        # flat copies of the default dataset (pre-multi-dataset frontend API)
        "experiments": first["experiments"],
        "variables": first["variables"],
        "levels": first["levels"],
        "members": first["members"],
        "ts_limits": {"months": TS_MAX_MONTHS},
        "box_limits": {"months": BOX_MAX_MONTHS or None, "values": BOX_MAX_VALUES},
    }


def _stat_token(stat: str, member: str) -> str:
    """Cache-key token for a (statistic, member) combination."""
    return {"raw": member, "mean": "ensmean", "spread": "ensspread"}.get(
        stat, f"anom{member}"
    )


def _select_stat(src, ds: xr.Dataset, var: str, member: str, time: str, stat: str) -> xr.DataArray:
    """One month of: a single member, the ensemble mean, the ensemble
    spread (sample std, ddof=1), or a member's deviation from the mean.
    Datasets without an ensemble dimension return the field itself."""
    da = ds[var].sel(time=slice(time, time))
    if da.sizes.get("time", 0) == 0:
        raise HTTPException(404, f"no timestep for {time}")
    da = da.isel(time=0)
    try:
        return src.reduce(da, stat, member)
    except KeyError:
        raise HTTPException(400, f"unknown member {member!r}")


def _level_group(src, ds, da, lev):
    """Stores that chunk the level dimension in groups (SPEAR: 9 levels per
    chunk) make one level cost the whole group's download. Return the
    loaded group containing `lev` (hPa) so every level in it can be cached."""
    ldim = src.level_dim
    lvals = ds[ldim].values
    idx = int(np.argmin(np.abs(lvals - lev * src.level_scale)))
    k = max(1, int(src.level_chunk))
    g0 = (idx // k) * k
    return _load_da(da.isel({ldim: slice(g0, min(g0 + k, lvals.size))}))


def _grid_meta(ds: xr.Dataset) -> dict:
    lon = ds.lon.values.astype(np.float64)
    lat = ds.lat.values.astype(np.float64)
    shift = int(np.searchsorted(lon, 180.0))
    return {
        "nlat": int(lat.size),
        "nlon": int(lon.size),
        "lat0": float(lat[0]),
        "dlat": float(lat[1] - lat[0]),
        "lon0": float(lon[shift] - 360.0),
        "dlon": float(lon[1] - lon[0]),
        "_shift": shift,
    }


def _load_values(da: xr.DataArray) -> np.ndarray:
    """Materialize a lazy array with retries — transient network stalls to
    S3 (icechunk 'observed throughput 0 B/s') otherwise fail the request."""
    last = None
    for attempt in range(3):
        try:
            return np.asarray(da.values)
        except Exception as e:
            last = e
            time_mod.sleep(1.5 * (attempt + 1))
    raise HTTPException(503, f"data store temporarily unreachable: {last}")


def _load_da(da: xr.DataArray) -> xr.DataArray:
    """da.load() with the same retry behaviour as _load_values."""
    last = None
    for attempt in range(3):
        try:
            return da.load()
        except Exception as e:
            last = e
            time_mod.sleep(1.5 * (attempt + 1))
    raise HTTPException(503, f"data store temporarily unreachable: {last}")


def _round_sig(vals: np.ndarray, sig: int = 5) -> np.ndarray:
    """Round to significant digits, not fixed decimals — fixed 2-decimal
    rounding destroyed small-magnitude fields (hus ~0.005 kg/kg)."""
    with np.errstate(all="ignore"):
        mags = np.floor(np.log10(np.abs(vals)))
    mags = np.where(np.isfinite(mags), mags, 0.0)
    factor = 10.0 ** (sig - 1 - mags)
    return np.round(vals * factor) / factor


def _extract(da: xr.DataArray, shift: int, scale: float, offset: float):
    """Scale to display units, roll lon 0..360 -> -180..180, JSON-ify."""
    vals = _load_values(da).astype(np.float64) * scale + offset
    vals = _round_sig(np.roll(vals, -shift, axis=-1))
    mask = np.isnan(vals)
    obj = vals.astype(object)
    obj[mask] = None
    vmin = None if mask.all() else float(np.nanmin(vals))
    vmax = None if mask.all() else float(np.nanmax(vals))
    return obj.tolist(), vmin, vmax


@app.get("/api/field")
def field(
    var: str = Query("pr"),
    experiment: str = Query("scenarioSSP5-85"),
    member: str = Query("r1i1p1f1"),
    time: str = Query("2030-01", pattern=r"^\d{4}-\d{2}$"),
    plev: float | None = Query(None, gt=0),
    stat: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
    dataset: str | None = Query(None),
):
    src = _src(dataset)
    ds = _open(src, experiment, var)
    if member == "ensmean" and stat == "raw":
        stat = "mean"  # back-compat with the pre-statistic API
    if not src.ensemble_dim:
        stat, member = "raw", ""

    cfg = src.variables[var]
    token = _stat_token(stat, member)
    lev = src.nearest_level(plev) if cfg["plev"] else None
    suffix = f"_p{lev}" if lev is not None else ""
    cache_path = CACHE_DIR / f"{src.id}_{var}_{experiment}_{token}_{time}{suffix}.json"
    cached = _cache_get(cache_path)
    if cached is not None:
        return cached

    da = _select_stat(src, ds, var, member, time, stat)
    grid = _grid_meta(ds)
    shift = grid.pop("_shift")
    # spread/deviation are difference-like: the unit offset cancels (a 2 K
    # spread is 2 °C of spread, not -271 °C).
    offset = 0.0 if stat in ("spread", "anom") else cfg["offset"]

    def payload_bytes(da2d, lev_k):
        values, vmin, vmax = _extract(da2d, shift, cfg["scale"], offset)
        payload = {
            "dataset": src.id, "var": var, "label": cfg["label"], "units": cfg["units"],
            "experiment": experiment, "member": member, "stat": stat, "time": time,
            "time_label": str(da2d.time.values),
            "plev": lev_k,
            **grid,
            "vmin": vmin, "vmax": vmax, "values": values,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    if lev is not None:
        ldim = src.level_dim
        group = _level_group(src, ds, da, lev)
        body = None
        for k in range(group.sizes[ldim]):
            da_k = group.isel({ldim: k})
            lev_k = src.level_hpa(da_k)
            body_k = payload_bytes(da_k, lev_k)
            _cache_put(CACHE_DIR / f"{src.id}_{var}_{experiment}_{token}_{time}_p{lev_k}.json", body_k)
            if lev_k == lev:
                body = body_k
        if body is None:
            body = payload_bytes(_load_da(src.level_select(da, lev)), lev)
    else:
        body = payload_bytes(_load_da(da), None)
        _cache_put(cache_path, body)
    return Response(body, media_type="application/json")


@app.get("/api/wind")
def wind(
    experiment: str = Query("scenarioSSP5-85"),
    member: str = Query("r1i1p1f1"),
    time: str = Query("2030-01", pattern=r"^\d{4}-\d{2}$"),
    plev: float | None = Query(None, gt=0),
    dataset: str | None = Query(None),
):
    """Wind components for the particle animation: the dataset's
    near-surface (u, v) pair by default, or its pressure-level pair at the
    requested level (hPa). 404 when the dataset has no wind components."""
    src = _src(dataset)
    pair = src.wind["plev"] if plev else src.wind["sfc"]
    if not pair:
        raise HTTPException(404, "this dataset has no wind components")
    uname, vname = pair
    ds = _open(src, experiment, uname)
    ds_v = _open(src, experiment, vname)
    lev = src.nearest_level(plev) if plev else None
    suffix = f"_p{lev}" if lev is not None else ""
    cache_path = CACHE_DIR / f"{src.id}_wind_{experiment}_{member}_{time}{suffix}.json"
    cached = _cache_get(cache_path)
    if cached is not None:
        return cached

    grid = _grid_meta(ds)
    shift = grid.pop("_shift")
    stat = "mean" if member == "ensmean" else "raw"
    if not src.ensemble_dim:
        stat, member = "raw", ""
    units = src.variables[uname]["units"] or "m/s"

    def wind_payload_bytes(da_u2d, da_v2d, lev_k):
        u_values, _, _ = _extract(da_u2d, shift, 1.0, 0.0)
        v_values, _, _ = _extract(da_v2d, shift, 1.0, 0.0)
        payload = {
            "units": units, "dataset": src.id, "experiment": experiment, "member": member,
            "time": time, "plev": lev_k, **grid,
            "u": u_values, "v": v_values,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    da_u = _select_stat(src, ds, uname, member, time, stat)
    da_v = _select_stat(src, ds_v, vname, member, time, stat)
    if lev is not None:
        ldim = src.level_dim
        u_group = _level_group(src, ds, da_u, lev)
        v_group = _level_group(src, ds_v, da_v, lev)
        body = None
        for k in range(u_group.sizes[ldim]):
            lev_k = src.level_hpa(u_group.isel({ldim: k}))
            body_k = wind_payload_bytes(u_group.isel({ldim: k}), v_group.isel({ldim: k}), lev_k)
            _cache_put(CACHE_DIR / f"{src.id}_wind_{experiment}_{member}_{time}_p{lev_k}.json", body_k)
            if lev_k == lev:
                body = body_k
        if body is None:
            body = wind_payload_bytes(_load_da(src.level_select(da_u, lev)),
                                      _load_da(src.level_select(da_v, lev)), lev)
    else:
        body = wind_payload_bytes(_load_da(da_u), _load_da(da_v), None)
        _cache_put(cache_path, body)
    return Response(body, media_type="application/json")


# ---------------------------------------------------------------- download
def _native_month(src, experiment: str, var: str, member: str, time: str,
                  plev: float | None = None, stat: str = "raw") -> xr.DataArray:
    """One month of the requested statistic in the store's native units
    and native 0-360 longitudes, with original attrs, loaded."""
    ds = _open(src, experiment, var)
    cfg = src.variables[var]
    if not src.ensemble_dim:
        stat, member = "raw", ""
    da = _select_stat(src, ds, var, member, time, stat)
    if cfg["plev"]:
        da = src.level_select(da, plev)
    da = _load_da(da)
    if src.ensemble_dim and src.ensemble_dim in da.coords:
        da = da.drop_vars(src.ensemble_dim)
    da.attrs["long_name"] = src.stat_long_name(stat, member, da.attrs.get("long_name", var))
    return da


def _sel_text(src, experiment: str, member: str, time: str,
              da: xr.DataArray = None, stat: str = "raw") -> str:
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    text = f"experiment {experiment}, {src.stat_text(stat, member)}, month {time}"
    lev = src.level_hpa(da) if da is not None else None
    if lev is not None:
        text += f", pressure level {lev} hPa"
    return text


def _global_attrs(src, var: str, base: dict, extra: dict) -> dict:
    """Download metadata: the source's own global attributes (base) plus
    what this viewer knows about the subset."""
    attrs = dict(base)
    attrs.update({
        "title": f"{src.title} subset: {var}",
        "source_store": src.store_text,
        "grid": src.grid_text,
        "time_calendar": src.calendar,
        "metadata_note": "global attributes above are inherited from the source "
                         "store; per-file and per-member fields (realization, "
                         "tracking_id, filename, history) are omitted — see "
                         "'selection' for what this subset contains",
        "history": f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: "
                   f"subset extracted by windy viewer (dataset {src.id})",
    })
    attrs.update(extra)
    return attrs


def _to_netcdf_bytes(ds_out: xr.Dataset) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    # The engine must create the file itself — an existing empty file is
    # not valid HDF5 and the write fails. (netcdf4 engine: h5netcdf needs
    # h5py.)
    Path(path).unlink()
    try:
        ds_out.to_netcdf(path, engine="netcdf4")
        return Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)


def _csv_attr(v) -> str:
    """Attr values can contain newlines (the store's `source` does), which
    would escape the '#' comment prefix — flatten them."""
    return " | ".join(str(v).splitlines())


def _to_csv_bytes(ds_out: xr.Dataset, columns: list[str]) -> bytes:
    buf = io.StringIO()
    for k, v in ds_out.attrs.items():
        buf.write(f"# {k}: {_csv_attr(v)}\n")
    for c in ("lat", "lon"):
        if c in ds_out.coords and ds_out[c].attrs:
            a = ds_out[c].attrs
            buf.write(f"# coordinate {c}: {a.get('long_name', c)}, "
                      f"units {a.get('units', '?')}, "
                      f"standard_name {a.get('standard_name', '?')}\n")
    for name in columns:
        da = ds_out[name]
        for k, v in da.attrs.items():
            buf.write(f"# column {name} {k}: {v}\n")
    buf.write("lat,lon," + ",".join(columns) + "\n")
    lat = ds_out.lat.values
    lon = ds_out.lon.values
    grids = [np.asarray(ds_out[name].values) for name in columns]
    for i in range(lat.size):
        for j in range(lon.size):
            vals = ",".join(
                "" if np.isnan(g[i, j]) else f"{g[i, j]:.6g}" for g in grids
            )
            buf.write(f"{lat[i]:.4f},{lon[j]:.4f},{vals}\n")
    return buf.getvalue().encode()


@app.get("/api/download")
def download(
    format: str = Query("nc", pattern="^(nc|csv)$"),
    var: str = Query("pr"),
    experiment: str = Query("scenarioSSP5-85"),
    member: str = Query("r1i1p1f1"),
    time: str = Query("2030-01", pattern=r"^\d{4}-\d{2}$"),
    plev: float | None = Query(None, gt=0),
    stat: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
    compare: bool = Query(False),
    experiment_b: str = Query("scenarioSSP5-85"),
    member_b: str = Query("r2i1p1f1"),
    time_b: str = Query("2030-01", pattern=r"^\d{4}-\d{2}$"),
    stat_b: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
    dataset: str | None = Query(None),
):
    """Download the currently-viewed subset (or A-B comparison) with
    descriptive metadata, in the store's native units and coordinates."""
    src = _src(dataset)
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    if member_b == "ensmean" and stat_b == "raw":
        stat_b = "mean"
    da_a = _native_month(src, experiment, var, member, time, plev, stat)
    lev = src.level_hpa(da_a)
    lev_suffix = f"_{lev}hPa" if lev is not None else ""

    if compare:
        da_b = _native_month(src, experiment_b, var, member_b, time_b, plev, stat_b)
        time_a_val = str(da_a.time.values) if "time" in da_a.coords else time
        time_b_val = str(da_b.time.values) if "time" in da_b.coords else time_b
        a_vals = da_a.drop_vars("time", errors="ignore")
        b_vals = da_b.drop_vars("time", errors="ignore")
        diff = a_vals - b_vals
        diff.attrs = dict(da_a.attrs)
        long_name = da_a.attrs.get("long_name", var)
        diff.attrs["long_name"] = f"difference (A minus B) of {long_name}"
        a_vals.attrs = dict(da_a.attrs)
        a_vals.attrs["long_name"] = f"{long_name} (selection A)"
        b_vals.attrs = dict(da_b.attrs)
        b_vals.attrs["long_name"] = f"{long_name} (selection B)"
        ds_out = xr.Dataset(
            {f"{var}_diff": diff, f"{var}_a": a_vals, f"{var}_b": b_vals}
        )
        same_exp = experiment == experiment_b
        base = src.download_attrs(experiment, var, include_experiment=same_exp)
        extra = {
            "title": f"{src.title} comparison subset: {var} (A minus B)",
            "comparison": "A - B (pointwise difference on the native grid)",
            "selection_A": _sel_text(src, experiment, member, time, da_a, stat),
            "selection_B": _sel_text(src, experiment_b, member_b, time_b, da_b, stat_b),
            "time_A_value": time_a_val,
            "time_B_value": time_b_val,
            "note": f"{var}_diff = {var}_a - {var}_b; all fields share the "
                    "native units and grid of the source store",
        }
        if not same_exp:
            # Experiment-level attrs differ between A and B; record each.
            for label, exp in (("A", experiment), ("B", experiment_b)):
                exp_attrs = src.experiment_attrs(exp)
                extra[f"experiment_{label}_id"] = exp_attrs.get("experiment_id", exp)
                extra[f"experiment_{label}_description"] = exp_attrs.get("experiment", "")
                extra[f"experiment_{label}_forcing"] = exp_attrs.get("forcing", "")
        ds_out.attrs = _global_attrs(src, var, base, extra)
        columns = [f"{var}_diff", f"{var}_a", f"{var}_b"]
        stem = (
            f"{var}_diff_{experiment}_{_stat_token(stat, member)}_{time}"
            f"_minus_{experiment_b}_{_stat_token(stat_b, member_b)}_{time_b}{lev_suffix}"
        )
    else:
        ds_out = da_a.to_dataset(name=var)
        ds_out.attrs = _global_attrs(src, var, src.download_attrs(experiment, var), {
            "selection": _sel_text(src, experiment, member, time, da_a, stat),
            "statistic": src.stat_text(stat, member),
            "time_value": str(da_a.time.values) if "time" in da_a.coords else time,
        })
        columns = [var]
        stem = f"{var}_{experiment}_{_stat_token(stat, member)}_{time}{lev_suffix}"

    if format == "nc":
        body = _to_netcdf_bytes(ds_out)
        media = "application/x-netcdf"
        filename = f"{stem}.nc"
    else:
        body = _to_csv_bytes(ds_out, columns)
        media = "text/csv"
        filename = f"{stem}.csv"

    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- time series
# EXPERIMENTAL: station time-series extraction. Time is chunked in steps
# of ONE month in the store, so a series costs one chunk download per
# month (x30 for ensemble statistics). Extraction runs as a background
# job with real progress, batched by BATCH_MONTHS.
# Hard cap: 10 years per extraction (any statistic). Env-overridable.
TS_MAX_MONTHS = int(os.environ.get("TS_MAX_MONTHS", "120"))
TS_MAX_STATIONS = 9
TS_BATCH_MONTHS = 24

# Station colors: must match STATION_PALETTE in app.js so pin badges,
# single-station plots and merged plots all agree.
TS_PALETTE = ["#3987e5", "#e8833a", "#2fbf71", "#e0575b", "#a06ee0",
              "#e5c43a", "#56c8d8", "#e06ea8", "#96a84c"]


def _station_color(sid: int) -> str:
    return TS_PALETTE[(sid - 1) % len(TS_PALETTE)] if sid > 0 else "#cfcfc8"
_ts_jobs: dict = {}
_ts_lock = threading.Lock()
_plot_lock = threading.Lock()


def _ts_month_list(start: str, end: str):
    y0, m0 = map(int, start.split("-"))
    y1, m1 = map(int, end.split("-"))
    n = (y1 - y0) * 12 + (m1 - m0) + 1
    out = []
    y, m = y0, m0
    for _ in range(max(0, n)):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _run_ts_job(job_id: str, p: dict):
    job = _ts_jobs[job_id]
    try:
        src = _src(p["dataset"])
        cfg = src.variables[p["var"]]
        ds = src.open(p["experiment"], p["var"])
        lat = ds.lat.values.astype(float)
        lon = ds.lon.values.astype(float)
        ilats, ilons, plats, plons = [], [], [], []
        for pt in p["points"]:
            i = int(np.clip(round((pt["lat"] - lat[0]) / (lat[1] - lat[0])), 0, lat.size - 1))
            j = int(round(((pt["lon"] % 360) - lon[0]) / (lon[1] - lon[0])) % lon.size)
            ilats.append(i)
            ilons.append(j)
            plats.append(float(lat[i]))
            plons.append(float(lon[j]))
        months = p["months"]
        stat = p["stat"]
        offset = 0.0 if stat in ("spread", "anom") else cfg["offset"]
        all_vals, all_times = [], []
        for b0 in range(0, len(months), TS_BATCH_MONTHS):
            if job.get("cancel"):
                job["error"] = "cancelled"
                job["done"] = True
                return
            batch = months[b0:b0 + TS_BATCH_MONTHS]
            da = ds[p["var"]].sel(time=slice(batch[0], batch[-1]))
            da = src.reduce(da, stat, p["member"])
            if cfg["plev"]:
                da = src.level_select(da, p.get("plev"))
            da = da.isel(
                lat=xr.DataArray(ilats, dims="station"),
                lon=xr.DataArray(ilons, dims="station"),
            )
            da = _load_da(da)
            all_times.extend(list(da.time.values))
            all_vals.append(np.asarray(da.values, dtype=np.float64))
            job["progress"] = min(0.97, (b0 + len(batch)) / len(months))
        data = np.concatenate(all_vals, axis=0) * cfg["scale"] + offset
        lev_txt = f" · {p['plev']} hPa" if cfg["plev"] else ""
        sids = p.get("station_ids") or [k + 1 for k in range(len(plats))]
        labels = [
            (f"St {sids[k]} ({_loc_str(plats[k], plons[k])})" if sids[k] > 0
             else f"Point ({_loc_str(plats[k], plons[k])})")
            for k in range(len(plats))
        ]
        colors = [_station_color(sids[k]) for k in range(len(plats))]
        job["result"] = {
            "cftimes": all_times,
            "x": [t.year + (t.month - 0.5) / 12 for t in all_times],
            "months": [f"{t.year:04d}-{t.month:02d}" for t in all_times],
            "values": data,
            "labels": labels,
            "colors": colors,
            "plats": plats,
            "plons": plons,
            "units": cfg["units"],
            "title": f"{src.stat_text(stat, p['member'])} — {p['var']} · "
                     f"{p['experiment']}{lev_txt}",
            "params": p,
        }
        job["png"] = _ts_plot_png(job["result"])
        job["progress"] = 1.0
        job["done"] = True
    except Exception as e:
        job["error"] = str(e)[:300]
        job["done"] = True


def _ts_plot_png(r) -> bytes:
    with _plot_lock:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9.4, 4.3), facecolor="#1a1a19")
        ax.set_facecolor("#222221")
        colors = r.get("colors") or [None] * r["values"].shape[1]
        for s in range(r["values"].shape[1]):
            ax.plot(r["x"], r["values"][:, s], lw=1.3, label=r["labels"][s],
                    color=colors[s])
        ax.legend(fontsize=8, facecolor="#2a2a29", edgecolor="#4a4a48",
                  labelcolor="#ddd", ncols=2)
        # x axis in calendar form (YYYY-MM), not fractional years
        from matplotlib.ticker import FuncFormatter

        def _month_fmt(x, _pos):
            yr = int(np.floor(x))
            m = int(round((x - yr) * 12 + 0.5))
            m = min(12, max(1, m))
            return f"{yr}-{m:02d}"

        ax.xaxis.set_major_formatter(FuncFormatter(_month_fmt))
        ax.tick_params(axis="x", labelsize=8)
        ax.set_title(r["title"], color="#eee", fontsize=11)
        ax.set_ylabel(r["units"], color="#c8c8c2")
        ax.tick_params(colors="#a8a8a2")
        for sp in ax.spines.values():
            sp.set_color("#4a4a48")
        ax.grid(color="#333331", lw=0.5, alpha=0.8)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=115, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()


@app.post("/api/timeseries/start")
def ts_start(payload: dict):
    src = _src(payload.get("dataset"))
    var = payload.get("var")
    experiment = payload.get("experiment")
    _open(src, experiment, var)
    stat = payload.get("stat", "raw")
    if stat not in STATS:
        raise HTTPException(400, f"unknown stat {stat!r}")
    member = payload.get("member", "r1i1p1f1")
    if not src.ensemble_dim:
        stat, member = "raw", ""
    points = payload.get("points") or []
    if not (1 <= len(points) <= TS_MAX_STATIONS):
        raise HTTPException(400, f"1-{TS_MAX_STATIONS} stations required")
    start, end = payload.get("start", ""), payload.get("end", "")
    exp = src.experiments[experiment]
    if not (exp["start"] <= start <= end <= exp["end"]):
        raise HTTPException(400, f"range must lie within {exp['start']}..{exp['end']}")
    months = _ts_month_list(start, end)
    if len(months) > TS_MAX_MONTHS:
        raise HTTPException(
            400,
            f"Range too long: max {TS_MAX_MONTHS} months "
            f"({TS_MAX_MONTHS // 12} years) per extraction.",
        )
    station_ids = payload.get("station_ids")
    if not (isinstance(station_ids, list) and len(station_ids) == len(points)
            and all(isinstance(s, int) and 0 <= s <= 99 for s in station_ids)):
        station_ids = None
    job_id = uuid.uuid4().hex[:12]
    params = {
        "dataset": src.id, "var": var, "experiment": experiment, "member": member,
        "stat": stat, "plev": src.nearest_level(payload.get("plev")) if src.variables[var]["plev"] else None,
        "points": points, "station_ids": station_ids,
        "months": months, "start": start, "end": end,
    }
    with _ts_lock:
        # keep only the most recent handful of jobs in memory
        for old in list(_ts_jobs)[:-8]:
            _ts_jobs.pop(old, None)
        _ts_jobs[job_id] = {"progress": 0.0, "done": False, "error": None, "cancel": False}
    threading.Thread(target=_run_ts_job, args=(job_id, params), daemon=True).start()
    return {"job": job_id, "months": len(months), "stations": len(points)}


@app.post("/api/timeseries/merge")
def ts_merge(payload: dict):
    """Combine several completed single-station extractions into one plot.
    They must share variable, statistic, level, scenario and time frame
    (the UI enforces a shared range; this validates it)."""
    ids = [j for j in (payload.get("jobs") or []) if isinstance(j, str)]
    results = []
    for jid in ids:
        j = _ts_jobs.get(jid)
        if not j or not j.get("done") or j.get("error") or "result" not in j:
            raise HTTPException(400, f"extraction {jid} is not available to merge")
        results.append(j["result"])
    if len(results) < 2:
        raise HTTPException(400, "need at least two extracted stations to merge")
    fp = results[0]["params"]

    def sig(r):
        p = r["params"]
        return (p.get("dataset"), p["var"], p["stat"], p.get("plev"), p["experiment"],
                p["member"], p["start"], p["end"])

    if any(sig(r) != sig(results[0]) for r in results[1:]):
        raise HTTPException(
            400, "plots must share variable, statistic, level, scenario, "
                 "member and time frame to be merged"
        )
    merged = {
        **results[0],
        "values": np.concatenate([r["values"] for r in results], axis=1),
        "labels": sum((r["labels"] for r in results), []),
        "colors": sum((r.get("colors") or [] for r in results), []),
        "plats": sum((r["plats"] for r in results), []),
        "plons": sum((r["plons"] for r in results), []),
        "params": {
            **fp,
            "points": sum((r["params"]["points"] for r in results), []),
            "station_ids": sum(((r["params"].get("station_ids")
                                 or [0] * len(r["plats"])) for r in results), []),
        },
    }
    job_id = uuid.uuid4().hex[:12]
    with _ts_lock:
        for old in list(_ts_jobs)[:-8]:
            _ts_jobs.pop(old, None)
        _ts_jobs[job_id] = {
            "progress": 1.0, "done": True, "error": None, "cancel": False,
            "result": merged, "png": _ts_plot_png(merged),
        }
    return {"job": job_id, "stations": len(merged["plats"])}


@app.post("/api/timeseries/cancel/{job_id}")
def ts_cancel(job_id: str):
    job = _ts_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job["cancel"] = True
    return {"ok": True}


@app.get("/api/timeseries/status/{job_id}")
def ts_status(job_id: str):
    job = _ts_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {"progress": job["progress"], "done": job["done"], "error": job["error"]}


@app.get("/api/timeseries/plot/{job_id}.png")
def ts_plot(job_id: str):
    job = _ts_jobs.get(job_id)
    if job is None or not job.get("done") or job.get("error"):
        raise HTTPException(404, "no plot for this job")
    return Response(job["png"], media_type="image/png")


@app.get("/api/timeseries/data/{job_id}")
def ts_data(job_id: str, format: str = Query("csv", pattern="^(csv|nc)$")):
    job = _ts_jobs.get(job_id)
    if job is None or not job.get("done") or job.get("error"):
        raise HTTPException(404, "no data for this job")
    r = job["result"]
    p = r["params"]
    src = _src(p["dataset"])
    cfg = src.variables[p["var"]]
    attrs = _global_attrs(src, p["var"], src.download_attrs(p["experiment"], p["var"]), {
        "title": f"{src.title} station time series: {p['var']}",
        "selection": f"experiment {p['experiment']}, {src.stat_text(p['stat'], p['member'])}"
                     + (f", pressure level {p['plev']} hPa" if cfg["plev"] else ""),
        "statistic": src.stat_text(p["stat"], p["member"]),
        "stations": "; ".join(
            f"station {k + 1}: {_loc_str(r['plats'][k], r['plons'][k])}"
            for k in range(len(r["plats"]))
        ),
        "period": f"{p['start']} to {p['end']} (monthly)",
        "units_note": f"values in display units: {r['units']}"
                      + (" (difference-like: no offset applied)"
                         if p["stat"] in ("spread", "anom") else ""),
    })
    stem = (f"ts_{p['var']}_{p['experiment']}_{_stat_token(p['stat'], p['member'])}"
            f"_{p['start']}_{p['end']}")
    if format == "nc":
        ds_out = xr.Dataset(
            {p["var"]: (("time", "station"), r["values"])},
            coords={
                "time": ("time", r["cftimes"]),
                "station": ("station", np.arange(1, len(r["plats"]) + 1)),
                "lat": ("station", r["plats"]),
                "lon": ("station", r["plons"]),
            },
        )
        ds_out[p["var"]].attrs = {"units": r["units"], "long_name": r["title"]}
        ds_out.attrs = attrs
        body = _to_netcdf_bytes(ds_out)
        return Response(body, media_type="application/x-netcdf",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.nc"'})
    buf = io.StringIO()
    for k, v in attrs.items():
        buf.write(f"# {k}: {_csv_attr(v)}\n")
    cols = [f"st{k + 1}" for k in range(len(r["plats"]))]
    buf.write("time," + ",".join(cols) + "\n")
    for i, mth in enumerate(r["months"]):
        row = ",".join(f"{r['values'][i, k]:.6g}" for k in range(len(cols)))
        buf.write(f"{mth},{row}\n")
    return Response(buf.getvalue().encode(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})


# ---------------------------------------------------------------- box subset
# Rectangular lat/lon subset over a month range, downloadable as NetCDF or
# CSV with the same provenance metadata as the single-month downloads.
# Runs as a background job like the station time series (one chunk read
# per month, x30 for ensemble statistics). Values stay in the store's
# native units, as /api/download does.
# No month cap by default (0): BOX_MAX_VALUES already bounds the size, so a
# small box can span the whole scenario. Env-overridable.
BOX_MAX_MONTHS = int(os.environ.get("BOX_MAX_MONTHS", "0"))
BOX_MAX_VALUES = int(float(os.environ.get("BOX_MAX_VALUES", "20e6")))
_box_jobs: dict = {}
_box_lock = threading.Lock()


def _box_indices(ds: xr.Dataset, box: dict):
    """Index slices covering a lat/lon box on the store grid.

    Longitudes are handled in the store's 0-360 convention. A box that
    straddles the Greenwich meridian maps to two lon slices ("wrap") which
    the job concatenates and re-labels as -180..180. A box narrower than a
    grid cell falls back to the cell nearest its centre."""
    lat = ds.lat.values.astype(float)
    lon = ds.lon.values.astype(float)
    s, n = sorted((float(box["south"]), float(box["north"])))
    w, e = float(box["west"]), float(box["east"])
    if e < w:
        w, e = e, w

    ilat = np.where((lat >= s) & (lat <= n))[0]
    if ilat.size == 0:
        ilat = np.array([int(np.argmin(np.abs(lat - (s + n) / 2)))])
    lat_sl = slice(int(ilat.min()), int(ilat.max()) + 1)

    if e - w >= 360:
        return lat_sl, [slice(0, lon.size)], False
    w360, e360 = w % 360, e % 360
    if w360 <= e360:
        ilon = np.where((lon >= w360) & (lon <= e360))[0]
        if ilon.size == 0:
            ilon = np.array([int(np.argmin(np.abs(lon - (w360 + e360) / 2)))])
        return lat_sl, [slice(int(ilon.min()), int(ilon.max()) + 1)], False
    hi = np.where(lon >= w360)[0]   # west part: w360..360
    lo = np.where(lon <= e360)[0]   # east part: 0..e360
    parts = []
    if hi.size:
        parts.append(slice(int(hi.min()), lon.size))
    if lo.size:
        parts.append(slice(0, int(lo.max()) + 1))
    if not parts:
        mid = ((w360 + e360 + 360) / 2) % 360
        j = int(np.argmin(np.abs(lon - mid)))
        return lat_sl, [slice(j, j + 1)], False
    return lat_sl, parts, len(parts) == 2


def _box_shape(ds: xr.Dataset, box: dict) -> tuple[int, int]:
    lat_sl, lon_sls, _ = _box_indices(ds, box)
    nlat = lat_sl.stop - lat_sl.start
    nlon = sum(sl.stop - sl.start for sl in lon_sls)
    return nlat, nlon


def _parse_box(raw) -> dict:
    try:
        box = {k: float(raw[k]) for k in ("south", "north", "west", "east")}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "box needs numeric south, north, west, east")
    if not all(np.isfinite(v) for v in box.values()):
        raise HTTPException(400, "box coordinates must be finite")
    box["south"], box["north"] = sorted((max(-90.0, box["south"]), min(90.0, box["north"])))
    if box["east"] - box["west"] > 360:
        box["east"] = box["west"] + 360
    return box


def _run_box_job(job_id: str, p: dict):
    job = _box_jobs[job_id]
    try:
        src = _src(p["dataset"])
        cfg = src.variables[p["var"]]
        ds = src.open(p["experiment"], p["var"])
        lat_sl, lon_sls, wrap = _box_indices(ds, p["box"])
        months = p["months"]
        stat = p["stat"]
        parts = []
        for b0 in range(0, len(months), TS_BATCH_MONTHS):
            if job.get("cancel"):
                job["error"] = "cancelled"
                job["done"] = True
                return
            batch = months[b0:b0 + TS_BATCH_MONTHS]
            da = ds[p["var"]].sel(time=slice(batch[0], batch[-1]))
            da = src.reduce(da, stat, p["member"])
            if cfg["plev"]:
                da = src.level_select(da, p.get("plev"))
            da = da.isel(lat=lat_sl)
            if len(lon_sls) == 1:
                da = da.isel(lon=lon_sls[0])
            else:
                da = xr.concat([da.isel(lon=sl) for sl in lon_sls], dim="lon")
            da = _load_da(da)
            if src.ensemble_dim and src.ensemble_dim in da.coords:
                da = da.drop_vars(src.ensemble_dim)
            parts.append(da)
            job["progress"] = min(0.97, (b0 + len(batch)) / len(months))
        da = xr.concat(parts, dim="time") if len(parts) > 1 else parts[0]
        da = da.transpose("time", "lat", "lon")
        if wrap:
            lon = da.lon.values.astype(float)
            da = da.assign_coords(lon=("lon", ((lon + 180) % 360) - 180, dict(da.lon.attrs)))
        da.attrs["long_name"] = src.stat_long_name(stat, p["member"], da.attrs.get("long_name", p["var"]))
        # Store-specific encodings (zarr chunks/compressors) must not leak
        # into the NetCDF writer; keep only the calendar encoding of time.
        da.encoding = {}
        for c in da.coords.values():
            keep = {k: v for k, v in c.encoding.items() if k in ("units", "calendar", "dtype")}
            c.encoding = keep
        job["result"] = {"da": da, "params": p, "wrap": wrap}
        job["progress"] = 1.0
        job["done"] = True
    except Exception as e:
        job["error"] = str(e)[:300]
        job["done"] = True


@app.get("/api/box/cells")
def box_cells(
    var: str = Query("pr"),
    experiment: str = Query("scenarioSSP5-85"),
    south: float = Query(...), north: float = Query(...),
    west: float = Query(...), east: float = Query(...),
    dataset: str | None = Query(None),
):
    """How many grid cells a box covers on the variable's grid (cheap:
    coordinates only), so the UI can show the size before extracting."""
    src = _src(dataset)
    box = _parse_box({"south": south, "north": north, "west": west, "east": east})
    ds = _open(src, experiment, var)
    lat_sl, lon_sls, wrap = _box_indices(ds, box)
    lat_all = ds.lat.values.astype(float)
    lon_all = ds.lon.values.astype(float)
    lat = lat_all[lat_sl]
    lon = np.concatenate([lon_all[sl] for sl in lon_sls])
    if wrap:
        lon = ((lon + 180) % 360) - 180
    return {
        "nlat": int(lat.size), "nlon": int(lon.size), "cells": int(lat.size * lon.size),
        # exact grid-cell centres that will be extracted (file convention)
        "lat_min": float(lat.min()), "lat_max": float(lat.max()),
        "lon_min": float(lon.min()), "lon_max": float(lon.max()),
        "dlat": round(float(abs(lat_all[1] - lat_all[0])), 4),
        "dlon": round(float(abs(lon_all[1] - lon_all[0])), 4),
        "wrap": bool(wrap),
    }


@app.post("/api/box/start")
def box_start(payload: dict):
    src = _src(payload.get("dataset"))
    var = payload.get("var")
    experiment = payload.get("experiment")
    ds = _open(src, experiment, var)
    stat = payload.get("stat", "raw")
    if stat not in STATS:
        raise HTTPException(400, f"unknown stat {stat!r}")
    member = payload.get("member", "r1i1p1f1")
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    if not src.ensemble_dim:
        stat, member = "raw", ""
    box = _parse_box(payload.get("box") or {})
    start, end = payload.get("start", ""), payload.get("end", "")
    exp = src.experiments[experiment]
    if not (exp["start"] <= start <= end <= exp["end"]):
        raise HTTPException(400, f"range must lie within {exp['start']}..{exp['end']}")
    months = _ts_month_list(start, end)
    if BOX_MAX_MONTHS and len(months) > BOX_MAX_MONTHS:
        raise HTTPException(
            400, f"Range too long: max {BOX_MAX_MONTHS} months "
                 f"({BOX_MAX_MONTHS // 12} years) per extraction.")
    nlat, nlon = _box_shape(ds, box)
    n_values = len(months) * nlat * nlon
    if n_values > BOX_MAX_VALUES:
        raise HTTPException(
            400, f"Box too large: {n_values:,} values ({nlat} x {nlon} cells x "
                 f"{len(months)} months) exceeds the {BOX_MAX_VALUES:,} limit. "
                 "Shrink the box or the time range.")
    job_id = uuid.uuid4().hex[:12]
    params = {
        "dataset": src.id, "var": var, "experiment": experiment, "member": member, "stat": stat,
        "plev": src.nearest_level(payload.get("plev")) if src.variables[var]["plev"] else None,
        "box": box, "months": months, "start": start, "end": end,
    }
    with _box_lock:
        # subsets can be large; keep only the last few in memory
        for old in list(_box_jobs)[:-3]:
            _box_jobs.pop(old, None)
        _box_jobs[job_id] = {"progress": 0.0, "done": False, "error": None, "cancel": False}
    threading.Thread(target=_run_box_job, args=(job_id, params), daemon=True).start()
    return {"job": job_id, "months": len(months), "cells": nlat * nlon}


@app.post("/api/box/cancel/{job_id}")
def box_cancel(job_id: str):
    job = _box_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job["cancel"] = True
    return {"ok": True}


@app.get("/api/box/status/{job_id}")
def box_status(job_id: str):
    job = _box_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {"progress": job["progress"], "done": job["done"], "error": job["error"]}


@app.get("/api/box/data/{job_id}")
def box_data(job_id: str, format: str = Query("nc", pattern="^(nc|csv)$")):
    job = _box_jobs.get(job_id)
    if job is None or not job.get("done") or job.get("error"):
        raise HTTPException(404, "no data for this job")
    r = job["result"]
    p = r["params"]
    da = r["da"]
    var = p["var"]
    src = _src(p["dataset"])
    box = p["box"]
    lev = src.level_hpa(da)
    lev_txt = f", pressure level {lev} hPa" if lev is not None else ""
    lev_suffix = f"_{lev}hPa" if lev is not None else ""
    lat = da.lat.values
    lon = da.lon.values
    attrs = _global_attrs(src, var, src.download_attrs(p["experiment"], var), {
        "title": f"{src.title} box subset: {var}",
        "selection": f"experiment {p['experiment']}, {src.stat_text(p['stat'], p['member'])}, "
                     f"months {p['start']} to {p['end']}{lev_txt}",
        "statistic": src.stat_text(p["stat"], p["member"]),
        "period": f"{p['start']} to {p['end']} (monthly, {da.sizes['time']} steps)",
        "box_requested": f"lat {box['south']:.3f} to {box['north']:.3f}, "
                         f"lon {box['west']:.3f} to {box['east']:.3f} (degrees east, as drawn)",
        "box_grid": f"{lat.size} lat x {lon.size} lon = {lat.size * lon.size} cells; "
                    f"lat {float(lat.min()):.3f} to {float(lat.max()):.3f}, "
                    f"lon {float(lon.min()):.3f} to {float(lon.max()):.3f}",
        "longitude_convention": ("-180..180 (this box straddles the Greenwich meridian)"
                                 if r["wrap"] else "0..360 (native store convention)"),
    })
    stem = (f"box_{var}_{p['experiment']}_{_stat_token(p['stat'], p['member'])}"
            f"_{p['start']}_{p['end']}{lev_suffix}")
    if format == "nc":
        ds_out = da.to_dataset(name=var)
        ds_out.attrs = attrs
        body = _to_netcdf_bytes(ds_out)
        return Response(body, media_type="application/x-netcdf",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.nc"'})
    import pandas as pd

    buf = io.StringIO()
    for k, v in attrs.items():
        buf.write(f"# {k}: {_csv_attr(v)}\n")
    for k, v in da.attrs.items():
        buf.write(f"# column {var} {k}: {_csv_attr(v)}\n")
    buf.write("# columns: time (YYYY-MM), lat (degrees_north), lon (degrees_east), "
              f"{var} ({da.attrs.get('units', 'units unknown')})\n")
    times = np.array([f"{t.year:04d}-{t.month:02d}" for t in da.time.values])
    vals = np.asarray(da.values, dtype=np.float64)
    nt, ni, nj = vals.shape
    T, I, J = np.meshgrid(np.arange(nt), np.arange(ni), np.arange(nj), indexing="ij")
    df = pd.DataFrame({
        "time": times[T.ravel()],
        "lat": np.round(lat[I.ravel()].astype(float), 4),
        "lon": np.round(lon[J.ravel()].astype(float), 4),
        var: vals.ravel(),
    })
    df.to_csv(buf, index=False, float_format="%.6g", na_rep="")
    return Response(buf.getvalue().encode(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})


def _ts_summary(job_id: str) -> str:
    """Compact per-station summary of a completed extraction for SPEAK."""
    job = _ts_jobs.get(job_id)
    if not job or not job.get("done") or job.get("error") or "result" not in job:
        return ""
    r = job["result"]
    p = r["params"]
    src = _src(p["dataset"])
    cfg = src.variables[p["var"]]
    lev = f", {p['plev']} hPa" if cfg["plev"] else ""
    lines = [
        f"{p['var']} ({src.stat_text(p['stat'], p['member'])}), "
        f"experiment {p['experiment']}{lev}, {p['start']} to {p['end']} "
        f"({len(r['months'])} monthly values), units {r['units']}"
    ]
    x = np.asarray(r["x"], dtype=float)
    for k in range(r["values"].shape[1]):
        y = r["values"][:, k]
        m = np.isfinite(y)
        if not m.any():
            lines.append(f"  Station {k + 1} ({_loc_str(r['plats'][k], r['plons'][k])}): all missing")
            continue
        imin, imax = int(np.nanargmin(y)), int(np.nanargmax(y))
        trend = np.polyfit(x[m], y[m], 1)[0] * 10  # per decade
        lines.append(
            f"  Station {k + 1} ({_loc_str(r['plats'][k], r['plons'][k])}): "
            f"mean {_fmt(float(np.nanmean(y)))}, "
            f"min {_fmt(float(y[imin]))} in {r['months'][imin]}, "
            f"max {_fmt(float(y[imax]))} in {r['months'][imax]}, "
            f"linear trend {trend:+.4g} {r['units']} per decade"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- chat
SYSTEM_PROMPT = (
    "You are SPEAK (the SPEAR Knowledge Assistant), embedded in an interactive "
    "map viewer for gridded monthly climate datasets; the DATASET section "
    "below describes the one currently on screen. Refer to yourself as SPEAK. "
    "Answer questions about the data currently on screen, the model or "
    "reanalysis it comes from, and its variables. "
    "Use the VIEW CONTEXT section for on-screen values (they are statistics "
    "computed from the displayed field, in the stated display units) and the "
    "DOCUMENTATION section for background. If an EXTRACTED TIME SERIES "
    "section is present, it summarizes monthly series the user extracted at "
    "their virtual stations (means, extremes with dates, linear trends per "
    "decade) — use it for trend, seasonality and variability questions about "
    "those stations. Be concise and quantitative. "
    "If something is not in the context or documentation, say so rather than "
    "guessing. These are monthly-mean gridded fields (model output or "
    "reanalysis, as the DATASET section says), not station observations or "
    "forecasts. Coordinates use hemisphere letters: °W longitudes are in "
    "the western hemisphere (the Americas side), °E in the eastern hemisphere "
    "(Europe/Africa/Asia side) — take care to name regions accordingly. The "
    "user-clicked point and the statistics' min/max locations are DIFFERENT "
    "places; never merge or interchange them, and when referring to the "
    "clicked point always restate its own coordinates. "
    "If the user asks where to find or download the data itself, use the "
    "DATASET section (and note this viewer's Download buttons export the "
    "currently displayed subset as NetCDF or CSV). "
    "When you provide an analysis or interpretation (trends, comparisons, "
    "physical explanations, significance judgements), end the response with a "
    "single footer line in this exact style: "
    "'Confidence: high|medium|low — Sources: <list>' where <list> names each "
    "basis used: 'on-screen statistics' (the VIEW CONTEXT numbers), the titles "
    "of any DOCUMENTATION excerpts you drew on (e.g. 'doc: <title>'), and/or "
    "'general knowledge' for background from training. Choose the confidence "
    "honestly: high when the on-screen numbers directly support the claim, "
    "medium when interpreting beyond the numbers, low when speculating or the "
    "context is insufficient. Skip the footer for simple factual or UI "
    "questions that need no analysis. "
    "Formatting: plain text with simple markdown only — **bold**, `code`, "
    "bullet lists starting with '* ', and [links](url). Never use LaTeX or "
    "math notation (no $...$, \\text, \\circ, subscripts) and no '#' "
    "headings; write units and coordinates as plain text, e.g. 3.1 mm/day, "
    "23°N–90°N. "
    "When you mention a publication, include its DOI as a markdown link "
    "(https://doi.org/...) if — and only if — you know the DOI with "
    "certainty from the documentation or established knowledge; never guess "
    "or fabricate a DOI, cite title and authors alone when unsure."
)


def _dataset_notes(src) -> str:
    """The DATASET section of the chat system prompt."""
    exps = "; ".join(f"{k} {v['start']} to {v['end']}" for k, v in src.experiments.items())
    lines = [f"Dataset id {src.id}: {src.title}" + (f" — {src.subtitle}" if src.subtitle else "")]
    if src.description:
        lines.append(src.description)
    lines.append(f"Experiments/periods: {exps}. Grid: {src.grid_text}. Calendar: {src.calendar}.")
    lines.append(f"Ensemble: {len(src.members)} members" if src.ensemble_dim
                 else "Ensemble: none (single realization; no member/mean/spread statistics)")
    lines.append("Variables: " + ", ".join(
        f"{k} ({v['long']}, displayed in {v['units']})" for k, v in src.variables.items()))
    if src.link:
        lines.append(f"Data access: {src.link}")
    if src.chat_notes:
        lines.append(src.chat_notes)
    return "\n".join(lines)


def _fmt(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.4g}"


def _loc_str(la, lo):
    lo180 = ((lo + 180) % 360) - 180
    return (f"{abs(la):.1f}°{'N' if la >= 0 else 'S'}, "
            f"{abs(lo180):.1f}°{'E' if lo180 >= 0 else 'W'}")


def _stats_text(vals, lat, lon, units, name):
    m = np.isfinite(vals)
    if not m.any():
        return f"{name}: all values missing"
    w = np.cos(np.deg2rad(lat))[:, None] * np.ones((1, lon.size))
    wm = np.where(m, w, 0.0)
    mean = float((np.where(m, vals, 0.0) * wm).sum() / wm.sum())
    masked = np.where(m, vals, np.nan)
    vmin_idx = np.unravel_index(np.nanargmin(masked), vals.shape)
    vmax_idx = np.unravel_index(np.nanargmax(masked), vals.shape)
    p10, p50, p90 = np.nanpercentile(masked, [10, 50, 90])

    def band(lo_deg, hi_deg):
        sel = (lat >= lo_deg) & (lat <= hi_deg)
        bw = wm[sel]
        if not sel.any() or bw.sum() == 0:
            return None
        return float((np.where(m[sel], vals[sel], 0.0) * bw).sum() / bw.sum())

    return (
        f"{name} [{units}]: area-weighted mean {_fmt(mean)}; "
        f"min {_fmt(float(vals[vmin_idx]))} at {_loc_str(lat[vmin_idx[0]], lon[vmin_idx[1]])}; "
        f"max {_fmt(float(vals[vmax_idx]))} at {_loc_str(lat[vmax_idx[0]], lon[vmax_idx[1]])}; "
        f"p10/p50/p90 {_fmt(p10)}/{_fmt(p50)}/{_fmt(p90)}; "
        f"band means 90S-23S {_fmt(band(-90, -23.5))}, "
        f"23S-23N {_fmt(band(-23.5, 23.5))}, 23N-90N {_fmt(band(23.5, 90))}"
    )


def _display_field(src, varname, experiment, member, time, plev, stat="raw"):
    cfg = src.variables[varname]
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    if not src.ensemble_dim:
        stat, member = "raw", ""
    ds = _open(src, experiment, varname)
    da = _select_stat(src, ds, varname, member, time, stat)
    if cfg["plev"]:
        da = src.level_select(da, plev)
    offset = 0.0 if stat in ("spread", "anom") else cfg["offset"]
    vals = _load_values(da).astype(np.float64) * cfg["scale"] + offset
    return vals, ds.lat.values.astype(float), ds.lon.values.astype(float)


def _point_value(vals, lat, lon, plat, plon):
    i = int(np.clip(round((plat - lat[0]) / (lat[1] - lat[0])), 0, lat.size - 1))
    j = int(round(((plon % 360) - lon[0]) / (lon[1] - lon[0])) % lon.size)
    v = vals[i, j]
    return float(v) if np.isfinite(v) else None


def _build_view_context(src, view):
    var = view.get("var") if view.get("var") in src.variables else next(iter(src.variables))
    cfg = src.variables[var]
    first_exp = next(iter(src.experiments))
    experiment = view.get("experiment") if view.get("experiment") in src.experiments else first_exp
    member = view.get("member", "r1i1p1f1")
    stat = view.get("stat", "raw")
    if stat not in STATS:
        stat = "raw"
    time = view.get("time") or src.experiments[experiment]["start"]
    plev = view.get("plev")
    lev_txt = f" at {plev} hPa" if cfg["plev"] and plev else ""
    mem_txt = src.stat_text("mean" if member == "ensmean" and stat == "raw" else stat, member)
    lines = [f"Variable: {var} — {cfg['long']} (displayed in {cfg['units']})"]
    try:
        va, lat, lon = _display_field(src, var, experiment, member, time, plev, stat)
    except Exception as e:
        return f"(view context unavailable: {e})"
    vb = None
    if view.get("compare"):
        eb = view.get("experiment_b", experiment)
        mb = view.get("member_b", "r2i1p1f1")
        sb = view.get("stat_b", "raw")
        if sb not in STATS:
            sb = "raw"
        tb = view.get("time_b", time)
        try:
            vb, _, _ = _display_field(src, var, eb, mb, tb, plev, sb)
        except Exception as e:
            return f"(view context unavailable for B: {e})"
        mb_txt = src.stat_text("mean" if mb == "ensmean" and sb == "raw" else sb, mb)
        lines.append(
            f"Compare mode: A = ({experiment}, {mem_txt}, {time}{lev_txt}) "
            f"minus B = ({eb}, {mb_txt}, {tb}{lev_txt})"
        )
        lines.append(_stats_text(va, lat, lon, cfg["units"], "A"))
        lines.append(_stats_text(vb, lat, lon, cfg["units"], "B"))
        lines.append(_stats_text(va - vb, lat, lon, cfg["units"], "Difference A-B"))
    else:
        lines.append(f"Selection: {experiment}, {mem_txt}, {time}{lev_txt}")
        lines.append(_stats_text(va, lat, lon, cfg["units"], "Displayed field"))
    click = view.get("click")
    if isinstance(click, dict) and "lat" in click:
        plat, plon = float(click["lat"]), float(click["lon"])
        lon180 = ((plon + 180) % 360) - 180
        hemi = "western hemisphere (Americas side)" if lon180 < 0 else \
               "eastern hemisphere (Europe/Africa/Asia side)"
        loc = f"{_loc_str(plat, plon)} — {hemi}, signed lon {lon180:.2f}"
        pa = _point_value(va, lat, lon, plat, plon)
        if vb is not None:
            pb = _point_value(vb, lat, lon, plat, plon)
            d = None if pa is None or pb is None else pa - pb
            lines.append(
                f"User-clicked point ({loc}): A={_fmt(pa)}, B={_fmt(pb)}, "
                f"difference={_fmt(d)} {cfg['units']}"
            )
        else:
            lines.append(f"User-clicked point ({loc}): {_fmt(pa)} {cfg['units']}")
    for i, pt in enumerate((view.get("points") or [])[:9]):
        try:
            plat, plon = float(pt["lat"]), float(pt["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        pa = _point_value(va, lat, lon, plat, plon)
        if vb is not None:
            pb = _point_value(vb, lat, lon, plat, plon)
            d = None if pa is None or pb is None else pa - pb
            lines.append(
                f"Virtual station #{i + 1} ({_loc_str(plat, plon)}): A={_fmt(pa)}, "
                f"B={_fmt(pb)}, difference={_fmt(d)} {cfg['units']}"
            )
        else:
            lines.append(
                f"Virtual station #{i + 1} ({_loc_str(plat, plon)}): {_fmt(pa)} {cfg['units']}"
            )
    return "\n".join(lines)


def _rag_retrieve(query):
    try:
        r = requests.post(f"{RAG_API_URL}/query", json={"query": query, "k": 3}, timeout=15)
        r.raise_for_status()
        parts = []
        for c in r.json().get("results", [])[:3]:
            md = c.get("metadata") or {}
            src = md.get("title") or md.get("source") or "doc"
            parts.append(f"[{src}] {c.get('content', '')[:1200]}")
        return "\n---\n".join(parts)
    except Exception:
        return ""  # chat still works without retrieval


def _gemini_call(model, system, messages, key):
    contents = [
        {"role": "user" if m["role"] == "user" else "model",
         "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        json={
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            # Gemini's thinking tokens count against maxOutputTokens; too
            # small a budget truncates analytical answers mid-sentence.
            "generationConfig": {
                "maxOutputTokens": 4096,
                "temperature": 0.4,
                "thinkingConfig": {"thinkingBudget": 1024},
            },
        },
        timeout=90,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _ollama_call(model, system, messages):
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 1500},
        },
        timeout=600,  # local models can be slow; don't cut them off
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def _anthropic_call(model, system, messages, key):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": model, "system": system, "max_tokens": 1500,
              "messages": messages},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


# Per-model quotas are independent, so on quota/transient errors we walk
# down this chain (older Gemini flash models, then Claude Haiku as a
# cross-vendor last resort). Google retires older models (404 "no longer
# available to new users"), so keep this list to currently served names.
FALLBACK_CHAIN = [
    ("gemini", None),  # None -> LLM_MODEL
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash"),
    ("anthropic", "claude-haiku-4-5-20251001"),
]


def _call_llm(system, messages):
    if LLM_PROVIDER == "anthropic":
        chain = [("anthropic", LLM_MODEL)]
    elif LLM_PROVIDER == "ollama":
        # local model first, cloud chain as safety net
        chain = [("ollama", LLM_MODEL)] + FALLBACK_CHAIN[1:]
    else:
        chain = FALLBACK_CHAIN
    last_err = None
    tried = set()
    for provider, model in chain:
        model = model or LLM_MODEL
        if (provider, model) in tried:
            continue
        tried.add((provider, model))
        key = None
        if provider != "ollama":
            key = os.environ.get(
                "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
            )
            if not key:
                continue
        try:
            if provider == "ollama":
                return _ollama_call(model, system, messages)
            if provider == "gemini":
                return _gemini_call(model, system, messages, key)
            return _anthropic_call(model, system, messages, key)
        except requests.HTTPError as e:
            last_err = e
            # quota exhausted, transient server error, or model retired
            # (404) -> try the next model
            if e.response.status_code in (404, 429, 500, 502, 503):
                continue
            raise
        except requests.RequestException as e:
            last_err = e
            continue
    if isinstance(last_err, requests.HTTPError):
        raise last_err
    raise HTTPException(502, f"no LLM available: {last_err}")


# ---- chat rate limiting (abuse / cost protection) -----------------------
# Layers: per-IP burst spacing, per-minute, per-day; a GLOBAL daily circuit
# breaker that bounds worst-case spend; and a message-size cap against
# token-stuffing. All env-configurable. In-memory: resets on restart and
# at UTC midnight.
CHAT_MIN_INTERVAL = float(os.environ.get("CHAT_MIN_INTERVAL_SECS", "4"))
CHAT_PER_MINUTE = int(os.environ.get("CHAT_PER_MINUTE", "8"))
CHAT_PER_DAY_IP = int(os.environ.get("CHAT_PER_DAY_IP", "150"))
CHAT_PER_DAY_GLOBAL = int(os.environ.get("CHAT_PER_DAY_GLOBAL", "2000"))
CHAT_MAX_CHARS = int(os.environ.get("CHAT_MAX_CHARS", "4000"))

_rate_lock = threading.Lock()
_rate_day = ""
_rate_ip: dict = {}
_rate_global = 0


def _client_ip(request: Request) -> str:
    # first hop of X-Forwarded-For when behind a reverse proxy
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_chat_rate(request: Request):
    global _rate_day, _rate_global
    ip = _client_ip(request)
    now = time_mod.time()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _rate_lock:
        if day != _rate_day:
            _rate_day = day
            _rate_ip.clear()
            _rate_global = 0
        if _rate_global >= CHAT_PER_DAY_GLOBAL:
            raise HTTPException(
                429, "SPEAK has reached its daily capacity — please try again tomorrow."
            )
        rec = _rate_ip.setdefault(ip, {"last": 0.0, "minute": [], "day": 0})
        if rec["day"] >= CHAT_PER_DAY_IP:
            raise HTTPException(
                429, "Daily chat limit reached for your connection — please try again tomorrow."
            )
        rec["minute"] = [t for t in rec["minute"] if now - t < 60]
        if len(rec["minute"]) >= CHAT_PER_MINUTE:
            raise HTTPException(429, "Too many messages this minute — please slow down.")
        if now - rec["last"] < CHAT_MIN_INTERVAL:
            raise HTTPException(429, "Please wait a few seconds between messages.")
        rec["last"] = now
        rec["minute"].append(now)
        rec["day"] += 1
        _rate_global += 1


def _chat_context(view: dict, query: str | None = None) -> list[tuple[str, str]]:
    """The context sections appended to the system prompt for a chat turn:
    dataset notes, on-screen statistics, extracted time series and (given
    the question) documentation excerpts."""
    src = _src(view.get("dataset"))
    sections = [("DATASET", _dataset_notes(src)),
                ("VIEW CONTEXT (current screen)", _build_view_context(src, view))]
    ts_ids = view.get("ts_jobs")
    if not isinstance(ts_ids, list):
        ts_ids = [view.get("ts_job")] if isinstance(view.get("ts_job"), str) else []
    summaries = []
    for i, tid in enumerate(ts_ids[:3]):
        if isinstance(tid, str):
            s = _ts_summary(tid)
            if s:
                tag = "most recent" if i == 0 else f"{i + 1} extractions ago"
                summaries.append(f"[Extraction {i + 1} — {tag}]\n{s}")
    if summaries:
        sections.append(("EXTRACTED TIME SERIES", "\n---\n".join(summaries)))
    if query:
        docs = _rag_retrieve(query)
        if docs:
            sections.append(("DOCUMENTATION EXCERPTS", docs))
    return sections


@app.post("/api/chat/context")
def chat_context(payload: dict):
    """What SPEAK would receive for the current view (no LLM call): lets
    the user inspect the statistics and notes behind the answers."""
    view = payload.get("view") or {}
    query = payload.get("query")
    query = query if isinstance(query, str) and query.strip() else None
    sections = _chat_context(view, query)
    return {"instructions": SYSTEM_PROMPT,
            "sections": [{"title": t, "text": txt} for t, txt in sections],
            "note": ("Documentation excerpts are retrieved per question; shown here for your last question."
                     if query else "Documentation excerpts are added per question once you ask one.")}


@app.post("/api/chat")
def chat(payload: dict, request: Request):
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, str) and len(content) > CHAT_MAX_CHARS:
            raise HTTPException(413, f"Message too long (max {CHAT_MAX_CHARS} characters).")
    _check_chat_rate(request)
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in payload.get("messages", [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]
    if not messages:
        raise HTTPException(400, "no messages")
    view = payload.get("view") or {}
    sections = _chat_context(view, messages[-1]["content"])
    system = SYSTEM_PROMPT + "".join(f"\n\n=== {t} ===\n{txt}" for t, txt in sections)
    try:
        reply = _call_llm(system, messages)
    except HTTPException:
        raise
    except requests.HTTPError as e:
        raise HTTPException(502, f"LLM error {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")
    return {"reply": reply}


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # 127.0.0.1 for local use; set VIEWER_HOST=0.0.0.0 in containers/cloud
    uvicorn.run(
        app,
        host=os.environ.get("VIEWER_HOST", "127.0.0.1"),
        port=int(os.environ.get("VIEWER_PORT", "8601")),
    )
