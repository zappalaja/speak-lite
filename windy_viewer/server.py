"""Local Windy-style viewer for SPEAR-MED.

Serves a Leaflet frontend plus a small JSON API that pulls single
(variable, experiment, member, month) fields from the ArrayLake repo
GFDL/noaa-gfdl-spear-large-ensembles-pds and caches them on disk.

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
CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

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

ARRAYLAKE_REPO = "GFDL/noaa-gfdl-spear-large-ensembles-pds"
ARRAYLAKE_BRANCH = "main"
FREQUENCY = "Amon"

EXPERIMENTS = {
    "historical": {"start": "1921-01", "end": "2014-12"},
    "scenarioSSP5-85": {"start": "2015-01", "end": "2100-12"},
}

# scale/offset convert stored units to display units. group selects the
# Zarr group ("Amon" or "Omon"); plev marks 3-D fields on pressure levels.
VARIABLES = {
    "pr": {
        "scale": 86400.0, "offset": 0.0, "units": "mm/day",
        "label": "Precipitation", "long": "Precipitation",
        "group": "Amon", "plev": False,
    },
    "tas": {
        "scale": 1.0, "offset": -273.15, "units": "°C",
        "label": "Temperature (2 m)", "long": "Near-Surface Air Temperature (2 m height)",
        "group": "Amon", "plev": False,
    },
    "psl": {
        # SPEAR stores psl already in hPa (not Pa as CMIP convention would
        # suggest) — verified against the store's units attribute.
        "scale": 1.0, "offset": 0.0, "units": "hPa",
        "label": "Sea-level pressure", "long": "Sea Level Pressure",
        "group": "Amon", "plev": False,
    },
    "sfcWind": {
        "scale": 1.0, "offset": 0.0, "units": "m/s",
        "label": "Wind speed (10 m)", "long": "Near-Surface Wind Speed (10 m height)",
        "group": "Amon", "plev": False,
    },
    "uas": {
        "scale": 1.0, "offset": 0.0, "units": "m/s",
        "label": "Zonal wind (10 m)", "long": "Eastward Near-Surface Wind (10 m height)",
        "group": "Amon", "plev": False,
    },
    "vas": {
        "scale": 1.0, "offset": 0.0, "units": "m/s",
        "label": "Meridional wind (10 m)", "long": "Northward Near-Surface Wind (10 m height)",
        "group": "Amon", "plev": False,
    },
    "rlut": {
        "scale": 1.0, "offset": 0.0, "units": "W/m²",
        "label": "OLR (TOA)", "long": "TOA Outgoing Longwave Radiation",
        "group": "Amon", "plev": False,
    },
    "rsut": {
        "scale": 1.0, "offset": 0.0, "units": "W/m²",
        "label": "Reflected SW (TOA)", "long": "TOA Outgoing Shortwave Radiation",
        "group": "Amon", "plev": False,
    },
    "rsdt": {
        "scale": 1.0, "offset": 0.0, "units": "W/m²",
        "label": "Incoming solar (TOA)", "long": "TOA Incident Shortwave Radiation",
        "group": "Amon", "plev": False,
    },
    "ta": {
        "scale": 1.0, "offset": -273.15, "units": "°C",
        "label": "Air temperature", "long": "Air Temperature (on pressure level)",
        "group": "Amon", "plev": True,
    },
    "ua": {
        "scale": 1.0, "offset": 0.0, "units": "m/s",
        "label": "Zonal wind", "long": "Eastward Wind (on pressure level)",
        "group": "Amon", "plev": True,
    },
    "va": {
        "scale": 1.0, "offset": 0.0, "units": "m/s",
        "label": "Meridional wind", "long": "Northward Wind (on pressure level)",
        "group": "Amon", "plev": True,
    },
    "zg": {
        "scale": 1.0, "offset": 0.0, "units": "m",
        "label": "Geopot. height", "long": "Geopotential Height (on pressure level)",
        "group": "Amon", "plev": True,
    },
    "hus": {
        "scale": 1.0, "offset": 0.0, "units": "kg/kg",
        "label": "Specific humidity", "long": "Specific Humidity (on pressure level)",
        "group": "Amon", "plev": True,
    },
    "tos": {
        "scale": 1.0, "offset": 0.0, "units": "°C",
        "label": "SST", "long": "Sea Surface Temperature (ocean, 1° grid)",
        "group": "Omon", "plev": False,
    },
}

_lock = threading.Lock()
_session = None
_datasets: dict[str, xr.Dataset] = {}


def _get_dataset(experiment: str, group_name: str = "Amon") -> xr.Dataset:
    global _session
    group = f"{experiment}/{group_name}"
    with _lock:
        if group not in _datasets:
            if _session is None:
                from arraylake import Client

                repo = Client().get_repo(ARRAYLAKE_REPO)
                _session = repo.readonly_session(branch=ARRAYLAKE_BRANCH)
            _datasets[group] = xr.open_zarr(
                _session.store, group=group, consolidated=False
            )
        return _datasets[group]


def _members(ds: xr.Dataset) -> list[str]:
    ids = [str(m) for m in ds.member_id.values]
    return sorted(ids, key=lambda m: int(m.split("i")[0][1:]))


app = FastAPI(title="SPEAR windy viewer")


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
    log = open(BASE_DIR / "data" / "rag.log", "ab")
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
        for exp in EXPERIMENTS:
            for grp in ("Amon", "Omon"):
                try:
                    _get_dataset(exp, grp)
                except Exception:
                    pass

    threading.Thread(target=warm, daemon=True).start()


@app.get("/api/meta")
def meta():
    ds = _get_dataset("scenarioSSP5-85")
    levels = [int(round(float(p) / 100)) for p in ds.plev.values.tolist()]
    experiments = {}
    for k, v in EXPERIMENTS.items():
        attrs = _get_dataset(k).attrs
        desc = attrs.get("experiment", k)
        forcing = attrs.get("forcing", "")
        experiments[k] = {**v, "long": f"{desc}" + (f" — forcing: {forcing}" if forcing else "")}
    return {
        "repo": ARRAYLAKE_REPO,
        "frequency": FREQUENCY,
        "experiments": experiments,
        "variables": {
            k: {kk: v[kk] for kk in ("units", "label", "long", "plev", "group")}
            for k, v in VARIABLES.items()
        },
        "levels": levels,
        "members": _members(ds),
        "ts_limits": {"months": TS_MAX_MONTHS},
    }


STATS = ("raw", "mean", "spread", "anom")


def _stat_token(stat: str, member: str) -> str:
    """Cache-key token for a (statistic, member) combination."""
    return {"raw": member, "mean": "ensmean", "spread": "ensspread"}.get(
        stat, f"anom{member}"
    )


def _select_stat(ds: xr.Dataset, var: str, member: str, time: str, stat: str) -> xr.DataArray:
    """One month of: a single member, the ensemble mean, the ensemble
    spread (sample std, ddof=1), or a member's anomaly from the mean."""
    da = ds[var].sel(time=slice(time, time))
    if da.sizes.get("time", 0) == 0:
        raise HTTPException(404, f"no timestep for {time}")
    da = da.isel(time=0)
    if stat == "mean":
        return da.mean("member_id", keep_attrs=True)
    if stat == "spread":
        return da.std("member_id", ddof=1, keep_attrs=True)
    if member not in set(str(m) for m in ds.member_id.values):
        raise HTTPException(400, f"unknown member {member!r}")
    if stat == "anom":
        res = da.sel(member_id=member) - da.mean("member_id")
        res.attrs = dict(da.attrs)
        return res
    return da.sel(member_id=member)


def _select_month(ds: xr.Dataset, var: str, member: str, time: str) -> xr.DataArray:
    da = ds[var].sel(time=slice(time, time))
    if da.sizes.get("time", 0) == 0:
        raise HTTPException(404, f"no timestep for {time}")
    da = da.isel(time=0)
    if member == "ensmean":
        # Average across all 30 members (reads one chunk per member).
        return da.mean("member_id", keep_attrs=True)
    if member not in set(str(m) for m in ds.member_id.values):
        raise HTTPException(400, f"unknown member {member!r}")
    return da.sel(member_id=member)


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
    plev: float = Query(500, gt=0),
    stat: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
):
    if var not in VARIABLES:
        raise HTTPException(400, f"unknown variable {var!r}")
    if experiment not in EXPERIMENTS:
        raise HTTPException(400, f"unknown experiment {experiment!r}")
    if member == "ensmean" and stat == "raw":
        stat = "mean"  # back-compat with the pre-statistic API

    cfg = VARIABLES[var]
    token = _stat_token(stat, member)
    suffix = f"_p{int(plev)}" if cfg["plev"] else ""
    cache_path = (
        CACHE_DIR / f"{var}_{experiment}_{cfg['group']}_{token}_{time}{suffix}.json"
    )
    cached = _cache_get(cache_path)
    if cached is not None:
        return cached

    ds = _get_dataset(experiment, cfg["group"])
    da = _select_stat(ds, var, member, time, stat)
    grid = _grid_meta(ds)
    shift = grid.pop("_shift")
    # spread/anomaly are difference-like: the unit offset cancels (a 2 K
    # spread is 2 °C of spread, not -271 °C).
    offset = 0.0 if stat in ("spread", "anom") else cfg["offset"]

    def payload_bytes(da2d, lev):
        values, vmin, vmax = _extract(da2d, shift, cfg["scale"], offset)
        payload = {
            "var": var, "label": cfg["label"], "units": cfg["units"],
            "experiment": experiment, "member": member, "stat": stat, "time": time,
            "time_label": str(da2d.time.values),
            "plev": lev,
            **grid,
            "vmin": vmin, "vmax": vmax, "values": values,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    if cfg["plev"]:
        # The store chunks the level dimension in groups of 9, so a single
        # level costs the whole group's download anyway. Load the group
        # once and cache every level in it — sibling levels become free.
        plevs = ds.plev.values
        idx = int(np.argmin(np.abs(plevs - plev * 100.0)))
        g0 = (idx // 9) * 9
        group = _load_da(da.isel(plev=slice(g0, min(g0 + 9, plevs.size))))
        body = None
        for k in range(group.sizes["plev"]):
            da_k = group.isel(plev=k)
            lev_k = int(round(float(da_k.plev.values) / 100))
            body_k = payload_bytes(da_k, lev_k)
            _cache_put(
                CACHE_DIR / f"{var}_{experiment}_{cfg['group']}_{token}_{time}_p{lev_k}.json",
                body_k,
            )
            if lev_k == int(round(float(plevs[idx]) / 100)):
                body = body_k
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
):
    """Wind components for the particle animation: near-surface (uas, vas)
    by default, or ua/va at the requested pressure level (hPa)."""
    if experiment not in EXPERIMENTS:
        raise HTTPException(400, f"unknown experiment {experiment!r}")

    suffix = f"_p{int(plev)}" if plev else ""
    cache_path = CACHE_DIR / f"wind_{experiment}_{FREQUENCY}_{member}_{time}{suffix}.json"
    cached = _cache_get(cache_path)
    if cached is not None:
        return cached

    ds = _get_dataset(experiment)
    grid = _grid_meta(ds)
    shift = grid.pop("_shift")

    def wind_payload_bytes(da_u2d, da_v2d, lev):
        u_values, _, _ = _extract(da_u2d, shift, 1.0, 0.0)
        v_values, _, _ = _extract(da_v2d, shift, 1.0, 0.0)
        payload = {
            "units": "m/s", "experiment": experiment, "member": member,
            "time": time, "plev": lev, **grid,
            "u": u_values, "v": v_values,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    if plev:
        # Cache the whole 9-level chunk group (see /api/field).
        plevs = ds.plev.values
        idx = int(np.argmin(np.abs(plevs - plev * 100.0)))
        g0 = (idx // 9) * 9
        sl = slice(g0, min(g0 + 9, plevs.size))
        u_group = _load_da(_select_month(ds, "ua", member, time).isel(plev=sl))
        v_group = _load_da(_select_month(ds, "va", member, time).isel(plev=sl))
        body = None
        for k in range(u_group.sizes["plev"]):
            lev_k = int(round(float(u_group.plev.values[k]) / 100))
            body_k = wind_payload_bytes(u_group.isel(plev=k), v_group.isel(plev=k), lev_k)
            _cache_put(
                CACHE_DIR / f"wind_{experiment}_{FREQUENCY}_{member}_{time}_p{lev_k}.json",
                body_k,
            )
            if lev_k == int(round(float(plevs[idx]) / 100)):
                body = body_k
    else:
        body = wind_payload_bytes(
            _load_da(_select_month(ds, "uas", member, time)),
            _load_da(_select_month(ds, "vas", member, time)),
            None,
        )
        _cache_put(cache_path, body)
    return Response(body, media_type="application/json")


# ---------------------------------------------------------------- download
def _native_month(
    experiment: str, var: str, member: str, time: str,
    plev: float | None = None, stat: str = "raw",
) -> xr.DataArray:
    """One month of the requested statistic in the store's native units
    and native 0-360 longitudes, with original attrs, loaded."""
    if experiment not in EXPERIMENTS:
        raise HTTPException(400, f"unknown experiment {experiment!r}")
    cfg = VARIABLES.get(var)
    if cfg is None:
        raise HTTPException(400, f"unknown variable {var!r}")
    ds = _get_dataset(experiment, cfg["group"])
    da = _select_stat(ds, var, member, time, stat)
    if cfg["plev"]:
        da = da.sel(plev=(plev or 500) * 100.0, method="nearest")
    da = _load_da(da)
    if "member_id" in da.coords:
        da = da.drop_vars("member_id")
    long = da.attrs.get("long_name", var)
    if stat == "spread":
        da.attrs["long_name"] = f"ensemble standard deviation (ddof=1, N=30) of {long}"
    elif stat == "anom":
        da.attrs["long_name"] = f"member {member} minus ensemble mean of {long}"
    elif stat == "mean":
        da.attrs["long_name"] = f"ensemble mean (30 members) of {long}"
    return da


def _stat_text(stat: str, member: str) -> str:
    return {
        "raw": f"member {member}",
        "mean": "ensemble mean of all 30 members",
        "spread": "ensemble spread (standard deviation, ddof=1, across 30 members)",
        "anom": f"anomaly: member {member} minus the 30-member ensemble mean",
    }[stat]


def _sel_text(experiment: str, member: str, time: str,
              da: xr.DataArray = None, stat: str = "raw") -> str:
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    text = f"experiment {experiment}, {_stat_text(stat, member)}, month {time}"
    if da is not None and "plev" in da.coords:
        text += f", pressure level {int(round(float(da.plev.values) / 100))} hPa"
    return text


# Original store attributes worth carrying into downloads. Member-specific
# keys (realization, parent_experiment_rip, tracking_id, per-file history,
# creation_date, run-numbered title) are deliberately omitted: the group's
# copies describe whichever single run seeded the Zarr consolidation, which
# is generally NOT the member being subset.
MODEL_LEVEL_KEYS = [
    "Conventions", "institution", "institute_id", "model_id", "modeling_realm",
    "product", "project_id", "source", "references", "license", "contact",
    "table_id", "initialization_method", "physics_version", "external_variables",
]
EXPERIMENT_LEVEL_KEYS = [
    "experiment", "experiment_id", "forcing", "branch_method", "parent_experiment_id",
]


def _source_attrs(ds: xr.Dataset, include_experiment: bool = True) -> dict:
    keys = MODEL_LEVEL_KEYS + (EXPERIMENT_LEVEL_KEYS if include_experiment else [])
    return {k: ds.attrs[k] for k in keys if k in ds.attrs}


def _global_attrs(var: str, src: dict, extra: dict) -> dict:
    attrs = dict(src)
    attrs.update({
        "title": f"SPEAR-MED subset: {var}",
        "source_store": f"arraylake://{ARRAYLAKE_REPO} (branch {ARRAYLAKE_BRANCH})",
        "grid": "0.5 deg lat x 0.625 deg lon (gr3), longitudes 0-360",
        "time_calendar": "julian",
        "metadata_note": "global attributes above are inherited from the source "
                         "store; member-specific source fields (realization, "
                         "tracking_id, per-file history) are omitted — see "
                         "'selection' for what this subset contains",
        "history": f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: "
                   "subset extracted by SPEAR windy viewer",
    })
    attrs.update(extra)
    return attrs


def _to_netcdf_bytes(ds_out: xr.Dataset) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    # The engine must create the file itself — an existing empty file is
    # not valid HDF5 and the write fails. (netcdf4 engine: h5netcdf needs
    # h5py, which is absent from the spear env.)
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
    plev: float = Query(500, gt=0),
    stat: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
    compare: bool = Query(False),
    experiment_b: str = Query("scenarioSSP5-85"),
    member_b: str = Query("r2i1p1f1"),
    time_b: str = Query("2030-01", pattern=r"^\d{4}-\d{2}$"),
    stat_b: str = Query("raw", pattern="^(raw|mean|spread|anom)$"),
):
    """Download the currently-viewed subset (or A-B comparison) with
    descriptive metadata, in the store's native units and coordinates."""
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    if member_b == "ensmean" and stat_b == "raw":
        stat_b = "mean"
    da_a = _native_month(experiment, var, member, time, plev, stat)
    lev_suffix = f"_{int(plev)}hPa" if VARIABLES[var]["plev"] else ""

    if compare:
        da_b = _native_month(experiment_b, var, member_b, time_b, plev, stat_b)
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
        grp = VARIABLES[var]["group"]
        src = _source_attrs(_get_dataset(experiment, grp), include_experiment=same_exp)
        extra = {
            "title": f"SPEAR-MED comparison subset: {var} (A minus B)",
            "comparison": "A - B (pointwise difference on the native grid)",
            "selection_A": _sel_text(experiment, member, time, da_a, stat),
            "selection_B": _sel_text(experiment_b, member_b, time_b, da_b, stat_b),
            "time_A_value": time_a_val,
            "time_B_value": time_b_val,
            "note": f"{var}_diff = {var}_a - {var}_b; all fields share the "
                    "native units and grid of the source store",
        }
        if not same_exp:
            # Experiment-level attrs differ between A and B; record each.
            for label, exp in (("A", experiment), ("B", experiment_b)):
                exp_attrs = _get_dataset(exp, grp).attrs
                extra[f"experiment_{label}_id"] = exp_attrs.get("experiment_id", exp)
                extra[f"experiment_{label}_description"] = exp_attrs.get("experiment", "")
                extra[f"experiment_{label}_forcing"] = exp_attrs.get("forcing", "")
        ds_out.attrs = _global_attrs(var, src, extra)
        columns = [f"{var}_diff", f"{var}_a", f"{var}_b"]
        stem = (
            f"{var}_diff_{experiment}_{_stat_token(stat, member)}_{time}"
            f"_minus_{experiment_b}_{_stat_token(stat_b, member_b)}_{time_b}{lev_suffix}"
        )
    else:
        ds_out = da_a.to_dataset(name=var)
        src = _source_attrs(_get_dataset(experiment, VARIABLES[var]["group"]))
        ds_out.attrs = _global_attrs(var, src, {
            "selection": _sel_text(experiment, member, time, da_a, stat),
            "statistic": _stat_text(stat, member),
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
# month (x30 for ensemble statistics) — extraction runs as a background
# job with real progress, batched by BATCH_MONTHS.
# Hard cap: 10 years per extraction (any statistic). Env-overridable.
TS_MAX_MONTHS = int(os.environ.get("TS_MAX_MONTHS", "120"))
TS_MAX_STATIONS = 9
TS_BATCH_MONTHS = 24

# Station colors — must match STATION_PALETTE in app.js so pin badges,
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
        cfg = VARIABLES[p["var"]]
        ds = _get_dataset(p["experiment"], cfg["group"])
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
            if stat == "mean":
                da = da.mean("member_id")
            elif stat == "spread":
                da = da.std("member_id", ddof=1)
            elif stat == "anom":
                da = da.sel(member_id=p["member"]) - da.mean("member_id")
            else:
                da = da.sel(member_id=p["member"])
            if cfg["plev"]:
                da = da.sel(plev=(p.get("plev") or 500) * 100.0, method="nearest")
            da = da.isel(
                lat=xr.DataArray(ilats, dims="station"),
                lon=xr.DataArray(ilons, dims="station"),
            )
            da = _load_da(da)
            all_times.extend(list(da.time.values))
            all_vals.append(np.asarray(da.values, dtype=np.float64))
            job["progress"] = min(0.97, (b0 + len(batch)) / len(months))
        data = np.concatenate(all_vals, axis=0) * cfg["scale"] + offset
        lev_txt = f" · {int(p['plev'])} hPa" if cfg["plev"] else ""
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
            "title": f"{_stat_text(stat, p['member'])} — {p['var']} · "
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
    var = payload.get("var")
    if var not in VARIABLES:
        raise HTTPException(400, f"unknown variable {var!r}")
    experiment = payload.get("experiment")
    if experiment not in EXPERIMENTS:
        raise HTTPException(400, f"unknown experiment {experiment!r}")
    stat = payload.get("stat", "raw")
    if stat not in STATS:
        raise HTTPException(400, f"unknown stat {stat!r}")
    points = payload.get("points") or []
    if not (1 <= len(points) <= TS_MAX_STATIONS):
        raise HTTPException(400, f"1-{TS_MAX_STATIONS} stations required")
    start, end = payload.get("start", ""), payload.get("end", "")
    exp = EXPERIMENTS[experiment]
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
        "var": var, "experiment": experiment, "member": payload.get("member", "r1i1p1f1"),
        "stat": stat, "plev": payload.get("plev"), "points": points,
        "station_ids": station_ids,
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
        return (p["var"], p["stat"], p.get("plev"), p["experiment"],
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
    cfg = VARIABLES[p["var"]]
    src = _source_attrs(_get_dataset(p["experiment"], cfg["group"]))
    attrs = _global_attrs(p["var"], src, {
        "title": f"SPEAR-MED station time series: {p['var']}",
        "selection": f"experiment {p['experiment']}, {_stat_text(p['stat'], p['member'])}"
                     + (f", pressure level {int(p['plev'])} hPa" if cfg["plev"] else ""),
        "statistic": _stat_text(p["stat"], p["member"]),
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


def _ts_summary(job_id: str) -> str:
    """Compact per-station summary of a completed extraction for SPEAK."""
    job = _ts_jobs.get(job_id)
    if not job or not job.get("done") or job.get("error") or "result" not in job:
        return ""
    r = job["result"]
    p = r["params"]
    cfg = VARIABLES[p["var"]]
    lev = f", {int(p['plev'])} hPa" if cfg["plev"] else ""
    lines = [
        f"{p['var']} ({_stat_text(p['stat'], p['member'])}), "
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
    "map viewer for the NOAA GFDL SPEAR-MED large ensemble (30 members, "
    "experiments: historical 1921-2014 and SSP5-8.5 2015-2100, monthly means, "
    "0.5°x0.625° atmosphere). Refer to yourself as SPEAK. Answer questions "
    "about the data currently on screen, the SPEAR model, and its variables. "
    "Use the VIEW CONTEXT section for on-screen values (they are statistics "
    "computed from the displayed field, in the stated display units) and the "
    "DOCUMENTATION section for background. If an EXTRACTED TIME SERIES "
    "section is present, it summarizes monthly series the user extracted at "
    "their virtual stations (means, extremes with dates, linear trends per "
    "decade) — use it for trend, seasonality and variability questions about "
    "those stations. Be concise and quantitative. "
    "If something is not in the context or documentation, say so rather than "
    "guessing. These are monthly-mean climate model fields, not observations "
    "or forecasts. Coordinates use hemisphere letters: °W longitudes are in "
    "the western hemisphere (the Americas side), °E in the eastern hemisphere "
    "(Europe/Africa/Asia side) — take care to name regions accordingly. The "
    "user-clicked point and the statistics' min/max locations are DIFFERENT "
    "places; never merge or interchange them, and when referring to the "
    "clicked point always restate its own coordinates. "
    "If the user asks where to find or download the SPEAR data "
    "itself, point them to the public AWS S3 bucket: "
    "https://noaa-gfdl-spear-large-ensembles-pds.s3.amazonaws.com/index.html"
    "#SPEAR/GFDL-LARGE-ENSEMBLES/CMIP/NOAA-GFDL/GFDL-SPEAR-MED/ "
    "(and note this viewer's Download buttons export the currently displayed "
    "subset as NetCDF or CSV). "
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
    "or fabricate a DOI, cite title and authors alone when unsure. The core "
    "SPEAR reference is Delworth et al. 2020, 'SPEAR: The Next Generation "
    "GFDL Modeling System for Seasonal to Multidecadal Prediction and "
    "Projection', J. Adv. Model. Earth Syst., "
    "[doi:10.1029/2019MS001895](https://doi.org/10.1029/2019MS001895)."
)


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


def _display_field(varname, experiment, member, time, plev, stat="raw"):
    cfg = VARIABLES[varname]
    if member == "ensmean" and stat == "raw":
        stat = "mean"
    ds = _get_dataset(experiment, cfg["group"])
    da = _select_stat(ds, varname, member, time, stat)
    if cfg["plev"]:
        da = da.sel(plev=(plev or 500) * 100.0, method="nearest")
    offset = 0.0 if stat in ("spread", "anom") else cfg["offset"]
    vals = _load_values(da).astype(np.float64) * cfg["scale"] + offset
    return vals, ds.lat.values.astype(float), ds.lon.values.astype(float)


def _point_value(vals, lat, lon, plat, plon):
    i = int(np.clip(round((plat - lat[0]) / (lat[1] - lat[0])), 0, lat.size - 1))
    j = int(round(((plon % 360) - lon[0]) / (lon[1] - lon[0])) % lon.size)
    v = vals[i, j]
    return float(v) if np.isfinite(v) else None


def _build_view_context(view):
    var = view.get("var") if view.get("var") in VARIABLES else "pr"
    cfg = VARIABLES[var]
    experiment = view.get("experiment", "scenarioSSP5-85")
    member = view.get("member", "r1i1p1f1")
    stat = view.get("stat", "raw")
    if stat not in STATS:
        stat = "raw"
    time = view.get("time", "2030-01")
    plev = view.get("plev")
    lev_txt = f" at {plev} hPa" if cfg["plev"] and plev else ""
    mem_txt = _stat_text("mean" if member == "ensmean" and stat == "raw" else stat, member)
    lines = [f"Variable: {var} — {cfg['long']} (displayed in {cfg['units']})"]
    try:
        va, lat, lon = _display_field(var, experiment, member, time, plev, stat)
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
            vb, _, _ = _display_field(var, eb, mb, tb, plev, sb)
        except Exception as e:
            return f"(view context unavailable for B: {e})"
        mb_txt = _stat_text("mean" if mb == "ensmean" and sb == "raw" else sb, mb)
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
# cross-vendor last resort).
FALLBACK_CHAIN = [
    ("gemini", None),  # None -> LLM_MODEL
    ("gemini", "gemini-3.5-flash"),
    ("gemini", "gemini-2.5-flash"),
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
            # quota exhausted or transient server error -> try the next model
            if e.response.status_code in (429, 500, 502, 503):
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
    context = _build_view_context(view)
    system = SYSTEM_PROMPT + "\n\n=== VIEW CONTEXT (current screen) ===\n" + context
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
        system += "\n\n=== EXTRACTED TIME SERIES ===\n" + "\n---\n".join(summaries)
    docs = _rag_retrieve(messages[-1]["content"])
    if docs:
        system += "\n\n=== DOCUMENTATION EXCERPTS ===\n" + docs
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
