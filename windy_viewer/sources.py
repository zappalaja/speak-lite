"""Data sources for the viewer.

A *source* is one dataset the viewer can display. Two kinds exist:

* ``ArrayLakeSource`` — any ArrayLake/Icechunk repo. The Zarr hierarchy is
  discovered automatically: every group holding ``(time, y, x)`` variables
  becomes an *experiment*; a regular ``lat``/``lon`` grid is used as is, a
  curvilinear grid (2-D ``geolat``/``geolon``) is regridded on the fly to a
  regular grid (nearest neighbour, index cached). Ensemble and level
  dimensions are picked up when present. The SPEAR large ensemble is the
  built-in default (with curated labels).
* ``CatalogSource`` — a local Icechunk store built by ``tools/nc_catalog.py``
  from a directory of NetCDF files (virtual chunk references into the
  files: nothing is copied). One Zarr group per variable, no ensemble.

Both expose the same description (experiments, variables, members, levels,
wind pairs, display-unit conversion) and the same ``open(experiment, var)``
returning an xarray Dataset whose ``var`` has dims
``(time[, <ensemble>][, <level>], lat, lon)`` with ascending lat — so
server.py never cares which kind it is talking to. Materialise arrays
through ``src.load(da)`` (retries + the regrid mask).

Sources are configured in a JSON file (``VIEWER_DATASETS``, default
``datasets.json`` next to the repo root); without one the built-in SPEAR
definition is used so the viewer behaves exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time as _time
import warnings
from pathlib import Path

import numpy as np
import xarray as xr

log = logging.getLogger("windy.sources")

# Icechunk's Rust logger warns on every local-filesystem commit/open
# ("not safe for concurrent commits"); one process writes here, so keep
# only errors unless the user set their own level.
os.environ.setdefault("ICECHUNK_LOG", "error")

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
# where regrid indexes are cached (next to the server's field cache)
INDEX_DIR = Path(os.environ.get("VIEWER_DATA_DIR") or BASE_DIR / "data") / "regrid"

# ---------------------------------------------------------------- units
# Native-unit string -> display conversion. Keyed on a normalised form of
# the units attribute so CMIP ("K", "kg m-2 s-1") and FMS ("deg_k",
# "kg/m2/s") spellings land on the same rule. `family` groups variables
# that share a set of selectable display units in the frontend
# (temperature, precipitation, wind, ...); anything unknown keeps its
# native units with an identity conversion.
_UNIT_RULES = [
    # (regex on normalised units, scale, offset, display units, family)
    (r"^(k|deg_?k|kelvin|degrees?_?k(elvin)?)$", 1.0, -273.15, "°C", "temp"),
    (r"^(deg_?c|celsius|degrees?_?c(elsius)?|°c|degc)$", 1.0, 0.0, "°C", "temp"),
    (r"^(kg/?m\^?-?2/?s\^?-?1|kgm-2s-1|kg/m2/s|kg/m\^2/s)$", 86400.0, 0.0, "mm/day", "precip"),
    (r"^(mm/day|mm/d|mmday-1)$", 1.0, 0.0, "mm/day", "precip"),
    (r"^(mm/hr|mm/h)$", 24.0, 0.0, "mm/day", "precip"),
    (r"^pa$", 0.01, 0.0, "hPa", "pressure"),
    (r"^(hpa|mb|mbar|millibar)$", 1.0, 0.0, "hPa", "pressure"),
    (r"^(m/s|ms-1|ms\^-1|m/sec|meters?/second|ms\^\{-1\})$", 1.0, 0.0, "m/s", "wind"),
    (r"^(w/?m\^?-?2|wm-2|w/m2|w/m\^2)$", 1.0, 0.0, "W/m²", "radiation"),
    (r"^(m|meters?|metres?)$", 1.0, 0.0, "m", "length"),
    (r"^(kg/kg|kgkg-1)$", 1.0, 0.0, "kg/kg", "ratio"),
    (r"^(%|percent)$", 1.0, 0.0, "%", "percent"),
    (r"^(psu|1e-3|0\.001|g/kg)$", 1.0, 0.0, "psu", "salinity"),
]


def _norm_units(u: str) -> str:
    u = (u or "").strip().lower()
    u = u.replace(" ", "").replace("**", "^")
    return u


def display_units(native: str) -> dict:
    """{'scale','offset','units','family','native'} for a native units string."""
    n = _norm_units(native)
    for rx, scale, offset, units, family in _UNIT_RULES:
        if re.match(rx, n):
            return {"scale": scale, "offset": offset, "units": units, "family": family, "native": native}
    return {"scale": 1.0, "offset": 0.0, "units": native or "", "family": "other", "native": native}


# ---------------------------------------------------------------- wind pairs
# (u, v) component names the particle animation can use, in priority order.
WIND_PAIRS = [("uas", "vas"), ("u_ref", "v_ref"), ("u10", "v10"), ("ssu", "ssv"), ("ua", "va"),
              ("ucomp", "vcomp"), ("uo", "vo"), ("u", "v")]

AUX_DIMS = {"time", "lat", "lon", "latitude", "longitude", "bnds", "nv", "x", "y"}
LEVEL_DIMS = ("plev", "pfull", "level", "lev", "pressure", "p", "z_l", "zl", "depth", "deptht", "z", "height")
ENSEMBLE_DIMS = ("member_id", "member", "ens", "ensemble", "realization", "run")
LATLON_2D = [("geolat", "geolon"), ("lat", "lon"), ("latitude", "longitude"), ("nav_lat", "nav_lon"),
             ("GEOLAT", "GEOLON"), ("TLAT", "TLON"), ("tlat", "tlon")]


def _retry(fn, attempts=3):
    """Call fn() with retries — transient network stalls to S3 otherwise
    fail the request."""
    last = None
    for k in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            _time.sleep(1.5 * (k + 1))
    raise last


def _ym(t) -> str:
    return f"{t.year:04d}-{t.month:02d}"


def _nice(x: float) -> float:
    r = round(x, 3)
    return int(r) if r == int(r) else r


# ---------------------------------------------------------------- regridding
class Regridder:
    """Nearest-neighbour map from a curvilinear (2-D lat/lon) grid onto a
    regular lat/lon grid at the native median spacing (capped at
    ``max_cells``). Applied lazily with xarray vectorised indexing, so only
    the chunks a request needs are read; the 'outside the domain' mask is
    applied when values are materialised (``Source.load``)."""

    def __init__(self, geolat: np.ndarray, geolon: np.ndarray, cache_file: Path | None = None,
                 max_cells: int = 1_500_000):
        self.cache_file = cache_file
        if cache_file and cache_file.exists():
            z = np.load(cache_file)
            self.lat, self.lon, iy, ix, self.mask = z["lat"], z["lon"], z["iy"], z["ix"], z["mask"]
        else:
            self.lat, self.lon, iy, ix, self.mask = self._build(geolat.astype(float), geolon.astype(float), max_cells)
            if cache_file:
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    np.savez(cache_file, lat=self.lat, lon=self.lon, iy=iy, ix=ix, mask=self.mask)
                except OSError as e:
                    log.warning("cannot cache regrid index at %s: %s", cache_file, e)
        self.IY = xr.DataArray(iy, dims=("lat", "lon"))
        self.IX = xr.DataArray(ix, dims=("lat", "lon"))

    @staticmethod
    def _build(gl, gn, max_cells):
        from scipy.spatial import cKDTree
        gn180 = ((gn + 180) % 360) - 180
        straddles_dateline = (np.nanmax(gn180) - np.nanmin(gn180)) > 300
        if straddles_dateline:
            gn180 = gn180 % 360   # work in 0..360 so the domain is contiguous
        dlat = float(np.nanmedian(np.abs(np.diff(gl, axis=0))))
        dlon = float(np.nanmedian(np.abs(np.diff(gn180, axis=1))))
        la0, la1 = float(np.nanmin(gl)), float(np.nanmax(gl))
        lo0, lo1 = float(np.nanmin(gn180)), float(np.nanmax(gn180))
        n = ((la1 - la0) / dlat) * ((lo1 - lo0) / dlon)
        if n > max_cells:  # coarsen uniformly to the cap
            f = (n / max_cells) ** 0.5
            dlat, dlon = dlat * f, dlon * f
        lat = np.arange(la0 + dlat / 2, la1, dlat)
        lon = np.arange(lo0 + dlon / 2, lo1, dlon)

        def xyz(la, lo):
            la, lo = np.radians(la), np.radians(lo)
            return np.c_[np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]

        ok = np.isfinite(gl.ravel()) & np.isfinite(gn.ravel())
        src_idx = np.flatnonzero(ok)
        tree = cKDTree(xyz(gl.ravel()[ok], gn180.ravel()[ok]))
        LA, LO = np.meshgrid(lat, lon, indexing="ij")
        dist, k = tree.query(xyz(LA.ravel(), LO.ravel()), workers=-1)
        flat = src_idx[k]
        iy, ix = np.unravel_index(flat, gl.shape)
        cell = np.sqrt(dlat ** 2 + dlon ** 2) * np.pi / 180  # ~one target cell, chord units
        mask = (dist > 1.5 * cell).reshape(LA.shape)       # True = outside the source domain
        # the viewer prefers 0..360 longitudes; keep -180..180 only when the
        # domain straddles 0° (so it stays contiguous)
        if lo1 < 0:
            lon = lon + 360.0
        return lat, lon, iy.reshape(LA.shape).astype(np.int32), ix.reshape(LA.shape).astype(np.int32), mask

    def apply(self, da: xr.DataArray, ydim: str, xdim: str) -> xr.DataArray:
        """Lazy regridded view. Dask's pointwise indexing merges the indexed
        dims into one chunk per combination of the other dims' chunks, so a
        4-D (time, level, y, x) array would materialise a whole
        (time chunk x level chunk) block to pick one level. Build the view
        per level instead and stack the pieces: selecting a level then only
        touches that level's (time chunk) block."""
        extra = [d for d in da.dims if d not in ("time", ydim, xdim)]
        if extra:
            d = extra[0]
            parts = [self.apply(da.isel({d: k}, drop=False), ydim, xdim) for k in range(da.sizes[d])]
            out = xr.concat(parts, dim=d, coords="minimal", compat="override")
            order = [x for x in da.dims if x not in (ydim, xdim)] + ["lat", "lon"]
            return out.transpose(*order)
        out = da.isel({ydim: self.IY, xdim: self.IX})
        drop = [c for c in out.coords if c not in ("lat", "lon", "time") and c not in out.dims
                and set(out[c].dims) & {"lat", "lon"}]
        return out.drop_vars(drop, errors="ignore").assign_coords(lat=("lat", self.lat), lon=("lon", self.lon))

    def gather(self, native: np.ndarray) -> np.ndarray:
        """Regrid a loaded native array (..., y, x) -> (..., lat, lon) with
        the index table (plain NumPy; 100 months take ~0.2 s)."""
        flat = np.asarray(native, dtype=np.float32).reshape(native.shape[:-2] + (-1,))
        idx = (self.IY.values * native.shape[-1] + self.IX.values).ravel()
        out = flat[..., idx].reshape(native.shape[:-2] + self.mask.shape)
        out[..., self.mask] = np.nan
        return out

    def masked(self, arr: np.ndarray, lat=None, lon=None) -> np.ndarray:
        """NaN outside the source domain. `lat`/`lon` (coordinate values of
        the array's last two dims) select the matching part of the mask for
        spatial subsets (box extractions)."""
        arr = np.array(arr, dtype=np.float32, copy=True)
        m = self.mask
        if lat is not None and lon is not None and (lat.size, lon.size) != m.shape:
            iy = np.clip(np.searchsorted(self.lat, lat), 0, self.lat.size - 1)
            ix = np.clip(np.searchsorted(self.lon, lon), 0, self.lon.size - 1)
            m = m[np.ix_(iy, ix)]
        arr[..., m] = np.nan
        return arr


# ---------------------------------------------------------------- base class
class Source:
    """Common description + access interface. Subclasses fill the fields
    in ``_load()`` (called lazily on first use; ArrayLake needs network)."""

    kind = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id: str = cfg["id"]
        self.title: str = cfg.get("title") or cfg["id"]
        self.subtitle: str = cfg.get("subtitle", "")
        self.description: str = cfg.get("description", "")
        self.link: str | None = cfg.get("link")      # "access the data" URL
        self.home: str | None = cfg.get("home")      # project homepage
        self.chat_notes: str = cfg.get("chat_notes", "")
        self.experiments: dict[str, dict] = {}
        self.variables: dict[str, dict] = {}
        self.members: list[str] = []
        self.levels: list[float] = []         # in display units (hPa, m, ...)
        self.level_units: str = "hPa"
        self.level_label: str = "Pressure level"
        self.ensemble_dim: str | None = None
        self.level_dim: str | None = None
        self.level_scale: float = 1.0         # native level units -> display units
        self.level_chunk: int = 1             # levels per chunk in the store
        self.time_chunk: int = 1              # time steps per chunk (block cache when > 1)
        self.wrap: bool = True                # global grid wrapping in longitude
        self.wind: dict = {"sfc": None, "plev": None}
        self.calendar: str = "standard"
        self.grid_text: str = ""
        self.store_text: str = ""
        self.regridder: Regridder | None = None
        self._loaded = False
        self._lock = threading.RLock()

    # -- lifecycle
    def ensure(self):
        with self._lock:
            if not self._loaded:
                self._load()
                self._finish()
                self._loaded = True
        return self

    def _load(self):
        raise NotImplementedError

    def _finish(self):
        """Derive wind pairs from the variable list unless configured."""
        w = self.cfg.get("wind")
        if w:
            self.wind = {"sfc": tuple(w["sfc"]) if w.get("sfc") else None,
                         "plev": tuple(w["plev"]) if w.get("plev") else None}
            return
        for u, v in WIND_PAIRS:
            if u in self.variables and v in self.variables:
                key = "plev" if self.variables[u]["plev"] else "sfc"
                if self.wind[key] is None:
                    self.wind[key] = (u, v)

    # -- description for /api/meta
    def meta(self) -> dict:
        self.ensure()
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "subtitle": self.subtitle,
            "description": self.description, "link": self.link, "home": self.home,
            "experiments": self.experiments,
            "variables": {k: {kk: v[kk] for kk in ("units", "native_units", "family", "label", "long",
                                                  "plev", "group", "start", "end")}
                          for k, v in self.variables.items()},
            "levels": self.levels,
            "level_units": self.level_units,
            "level_label": self.level_label,
            "members": self.members,
            "ensemble": bool(self.ensemble_dim),
            "n_members": len(self.members),
            "wind": {k: list(v) if v else None for k, v in self.wind.items()},
            "calendar": self.calendar,
            "grid": self.grid_text,
            "wrap": self.wrap,
            "time_chunk": self.time_chunk,
        }

    # -- data access
    def open(self, experiment: str, var: str) -> xr.Dataset:
        """Lazy dataset containing `var` (and lat/lon/time coords)."""
        raise NotImplementedError

    def read_block(self, experiment: str, var: str, t0: int, t1: int, stat: str, member: str | None,
                   lev: float | None) -> np.ndarray:
        """Loaded float32 (time, lat, lon) block for time indices t0:t1 of
        one variable/statistic/level. Generic version goes through the
        lazy dataset; sources with a cheaper path override it."""
        ds = self.open(experiment, var)
        blk = self.level_select(self.reduce(ds[var].isel(time=slice(t0, t1)), stat, member), lev)
        return self.load(blk).values.astype(np.float32)

    def load(self, da: xr.DataArray) -> xr.DataArray:
        """Materialise a lazy array (with retries) and apply the regrid mask."""
        out = _retry(lambda: da.load())
        if self.regridder is not None and {"lat", "lon"} <= set(out.dims) and out.dims[-2:] == ("lat", "lon"):
            out = out.copy(data=self.regridder.masked(out.values, out.lat.values, out.lon.values))
        return out

    def download_attrs(self, experiment: str, var: str, include_experiment: bool = True) -> dict:
        """Global attributes to carry into NetCDF/CSV downloads."""
        return {}

    def experiment_attrs(self, experiment: str) -> dict:
        return {}

    # -- helpers shared by the endpoints
    def var_cfg(self, var: str) -> dict:
        self.ensure()
        cfg = self.variables.get(var)
        if cfg is None:
            raise KeyError(var)
        return cfg

    def nearest_level(self, lev: float | None) -> float | None:
        """The available level (display units) closest to the request;
        500 hPa for pressure levels, the first (surface) level otherwise."""
        if not self.levels:
            return None
        if lev is None:
            return 500 if 500 in self.levels else self.levels[0]
        return min(self.levels, key=lambda p: abs(p - lev))

    def level_select(self, da: xr.DataArray, lev: float | None) -> xr.DataArray:
        """Select one level (given in display units) on a level-bearing array."""
        if not self.level_dim or self.level_dim not in da.dims:
            return da
        target = self.nearest_level(lev) * self.level_scale
        return da.sel({self.level_dim: target}, method="nearest")

    def level_value(self, da: xr.DataArray) -> float | None:
        """The level (display units) an array has been reduced to, or None."""
        if self.level_dim and self.level_dim in da.coords and da[self.level_dim].ndim == 0:
            return _nice(float(da[self.level_dim].values) / self.level_scale)
        return None

    def level_text(self, lev: float | None) -> str:
        return "" if lev is None else f"{self.level_label.lower()} {lev:g} {self.level_units}"

    def reduce(self, da: xr.DataArray, stat: str, member: str | None) -> xr.DataArray:
        """Apply the ensemble statistic. Without an ensemble dimension every
        statistic is the field itself ("raw")."""
        dim = self.ensemble_dim
        if not dim or dim not in da.dims:
            return da
        attrs = dict(da.attrs)
        if stat == "mean":
            return da.mean(dim, keep_attrs=True)
        if stat == "spread":
            return da.std(dim, ddof=1, keep_attrs=True)
        if member not in set(str(m) for m in da[dim].values):
            raise KeyError(member)
        if stat == "anom":
            res = da.sel({dim: member}) - da.mean(dim)
            res.attrs = attrs
            return res
        return da.sel({dim: member})

    def stat_text(self, stat: str, member: str | None) -> str:
        n = len(self.members)
        if not self.ensemble_dim:
            return "single realization"
        return {
            "raw": f"member {member}",
            "mean": f"ensemble mean of all {n} members",
            "spread": f"ensemble spread (standard deviation, ddof=1, across {n} members)",
            "anom": f"deviation: member {member} minus the {n}-member ensemble mean",
        }[stat]

    def stat_long_name(self, stat: str, member: str | None, long: str) -> str:
        n = len(self.members)
        if not self.ensemble_dim:
            return long
        return {
            "spread": f"ensemble standard deviation (ddof=1, N={n}) of {long}",
            "anom": f"member {member} minus ensemble mean of {long}",
            "mean": f"ensemble mean ({n} members) of {long}",
        }.get(stat, long)


def _var_entry(name: str, attrs: dict, dims, level_dim, group, start, end, override: dict | None = None) -> dict:
    """Build the per-variable description from attrs (+ config overrides)."""
    ov = override or {}
    native = ov.get("native_units", attrs.get("units", ""))
    du = display_units(native)
    long = ov.get("long") or attrs.get("long_name") or attrs.get("standard_name") or name
    return {
        "scale": ov.get("scale", du["scale"]), "offset": ov.get("offset", du["offset"]),
        "units": ov.get("units", du["units"]), "family": ov.get("family", du["family"]),
        "native_units": native,
        "label": ov.get("label") or long, "long": long,
        "plev": bool(level_dim and level_dim in dims),
        "group": ov.get("group", group),
        "start": start, "end": end,
    }


def _level_info(coord: xr.DataArray) -> tuple[float, str, str]:
    """(scale to display units, display units, label) for a level coordinate."""
    u = _norm_units(coord.attrs.get("units", ""))
    pos = str(coord.attrs.get("positive", "")).lower()
    if u == "pa":
        return 100.0, "hPa", "Pressure level"
    if u in ("hpa", "mb", "mbar", "millibar"):
        return 1.0, "hPa", "Pressure level"
    if u in ("m", "meters", "metres", "meter", "metre"):
        return 1.0, "m", "Height" if pos == "up" or str(coord.name) == "height" else "Depth"
    if u == "km":
        return 1.0, "km", "Height" if pos == "up" else "Depth"
    return 1.0, coord.attrs.get("units", ""), "Level"


def _dim_aliases(ds: xr.Dataset, ydim: str, xdim: str) -> dict[str, str]:
    """Other dimension names that index exactly the same cell centres as
    (ydim, xdim) — MOM6 writes sea-ice fields on yT/xT with the same
    coordinate values as the tracer grid's yh/xh."""
    out = {}
    for ref in (ydim, xdim):
        if ref not in ds.coords:
            continue
        rv = ds[ref].values
        for d in ds.dims:
            if d != ref and d in ds.coords and ds[d].ndim == 1 and ds[d].size == rv.size \
                    and np.allclose(ds[d].values, rv, atol=1e-6):
                out[d] = ref
    return out


def _looks_like_level(ds: xr.Dataset, dim: str) -> bool:
    if dim not in ds.coords or ds[dim].ndim != 1:
        return False
    u = _norm_units(ds[dim].attrs.get("units", ""))
    return u in ("pa", "hpa", "mb", "m", "meters", "metres", "km") or ds[dim].attrs.get("axis") == "Z"


def _select_vars(names: list[str], cfg: dict) -> list[str]:
    """Apply the config's `variables` (whitelist with overrides), `include`
    (names or regexes) and `max_variables` to the discovered names."""
    if cfg.get("variables"):
        missing = [v for v in cfg["variables"] if v not in names]
        if missing:
            log.warning("%s: configured variables not found in the store: %s", cfg.get("id"), ", ".join(missing))
        return [v for v in cfg["variables"] if v in names]
    inc = cfg.get("include")
    if inc:
        pats = [re.compile(f"^{p}$") for p in inc]
        names = [v for v in names if any(p.match(v) for p in pats)]
    cap = int(cfg.get("max_variables", 80))
    if len(names) > cap:
        log.warning("%s: %d variables discovered; showing the first %d (alphabetical). "
                    "Set 'variables' or 'include' in datasets.json to choose.", cfg.get("id"), len(names), cap)
        names = sorted(names)[:cap]
    return names


# ---------------------------------------------------------------- ArrayLake
class ArrayLakeSource(Source):
    """Any ArrayLake repo. Config::

        {"id": "cefi", "type": "arraylake", "repo": "NOAA-PMEL/cefi-nwa-hindcast-monthly",
         "branch": "main",
         "experiments": {...}, "groups": [...],     # optional: SPEAR-style <experiment>/<group>
                                                    #   layout with known periods; omitted = discover
         "variables": {"tos": {"label": "SST"}, ...},   # optional whitelist + overrides
         "include": ["tos", "so.*"],                # optional: names/regexes (when no whitelist)
         "max_variables": 80}

    Discovery walks the Zarr hierarchy: each group with ``(time, y, x)``
    variables is an experiment (named by its path). A 2-D lat/lon grid is
    regridded to a regular one on the fly.
    """

    kind = "arraylake"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.repo = cfg["repo"]
        self.branch = cfg.get("branch", "main")
        self._session = None
        self._datasets: dict[str, xr.Dataset] = {}      # native datasets by zarr path
        self._regridded: dict[str, xr.Dataset] = {}     # regridded single-variable views by "path::var"
        self._exp_path: dict[str, dict[str, str]] = {}  # experiment -> {group key: zarr path}
        self._var_group: dict[str, str] = {}            # var -> group key
        self._hdims: tuple[str, str] | None = None      # native horizontal dims when curvilinear
        self._dim_alias: dict[str, str] = {}            # e.g. {"yT": "yh", "xT": "xh"}: same cells, other names
        self.store_text = f"arraylake://{self.repo} (branch {self.branch})"

    # -- store access
    def _store(self):
        with self._lock:
            if self._session is None:
                from arraylake import Client
                repo = _retry(lambda: Client().get_repo(self.repo))
                self._session = repo.readonly_session(branch=self.branch)
            return self._session.store

    def _native(self, path: str) -> xr.Dataset:
        with self._lock:
            if path not in self._datasets:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # dask-backed: the regrid view (vectorised indexing) is built
                    # once per variable and later time/level selections only
                    # touch the chunks they need. (xarray's own lazy indexing
                    # would broadcast the index arrays to the full 4-D shape.)
                    self._datasets[path] = xr.open_zarr(self._store(), group=path or None, consolidated=False,
                                                        use_cftime=True, decode_timedelta=False)
            return self._datasets[path]

    def _walk(self, max_depth=3) -> list[str]:
        """Zarr group paths that look like datasets (have a time dim and a
        3-D+ data variable)."""
        import zarr
        root = zarr.open_group(self._store(), mode="r")
        found = []

        def visit(g, path, depth):
            if list(g.array_keys()):
                try:
                    ds = self._native(path)
                    if "time" in ds.dims and any(da.ndim >= 3 for da in ds.data_vars.values()):
                        found.append(path)
                        return
                except Exception as e:  # noqa: BLE001
                    log.debug("%s: cannot open %r: %s", self.id, path, e)
            if depth < max_depth:
                for k in g.group_keys():
                    visit(g[k], f"{path}/{k}" if path else k, depth + 1)

        visit(root, "", 0)
        return found

    # -- discovery
    def _load(self):
        cfg = self.cfg
        overrides = cfg.get("variables") or {}
        if cfg.get("experiments"):
            groups = cfg.get("groups", ["Amon"])
            for name, rng in cfg["experiments"].items():
                self._exp_path[name] = {g: f"{name}/{g}" for g in groups}
                attrs = self._native(f"{name}/{groups[0]}").attrs
                desc = attrs.get("experiment", name)
                forcing = attrs.get("forcing", "")
                self.experiments[name] = {**rng, "long": f"{desc}" + (f" — forcing: {forcing}" if forcing else "")}
        else:
            paths = self._walk()
            if not paths:
                raise RuntimeError(f"no (time, y, x) datasets found in {self.repo}")
            for p in paths:
                ds = self._native(p)
                name = p or "root"
                t = ds.time.values
                self.experiments[name] = {"start": _ym(t[0]), "end": _ym(t[-1]),
                                          "short": p.split("/")[-1] if p else "root",
                                          "long": str(ds.attrs.get("title") or ds.attrs.get("experiment") or name)}
                self._exp_path[name] = {"": p}
        first_exp = next(iter(self.experiments))
        first_paths = self._exp_path[first_exp]

        # grid, levels, ensemble, variables: from the first experiment's groups
        discovered: dict[str, tuple[str, dict, tuple]] = {}   # var -> (group key, attrs, dims)
        for gkey, path in first_paths.items():
            ds = self._native(path)
            hd = self._horizontal(ds)
            if hd is None:
                log.warning("%s: group %r has no recognisable lat/lon grid; skipped", self.id, path)
                continue
            ydim, xdim, regrid = hd
            if regrid is not None and self.regridder is None:
                self.regridder = regrid
                self._hdims = (ydim, xdim)
                self.wrap = False
            aliases = _dim_aliases(ds, ydim, xdim)
            self._dim_alias.update(aliases)
            # first pass: classify every plain (time[, level][, member], y, x) field
            fields = {}
            for v, da in ds.data_vars.items():
                dims = tuple(aliases.get(d, d) for d in da.dims)
                if "time" not in dims or dims[-2:] != (ydim, xdim):
                    continue
                extra = [d for d in dims if d not in ("time", ydim, xdim)]
                lev = next((d for d in extra if d in LEVEL_DIMS or _looks_like_level(ds, d)), None)
                ens = next((d for d in extra if d in ENSEMBLE_DIMS or d.startswith(("member", "ens"))), None)
                if [d for d in extra if d not in (lev, ens)]:
                    continue  # ice categories, bounds, ...: not a plain field
                fields[v] = (da, lev, ens)
            # one level axis per dataset: the one most variables use (named
            # level dims first), e.g. MOM6's z_l rather than the interface zi
            counts = {}
            for da, lev, _ in fields.values():
                if lev:
                    counts[lev] = counts.get(lev, 0) + 1
            if counts and not self.level_dim:
                best = max(counts, key=lambda d: (d in LEVEL_DIMS, counts[d]))
                self.level_dim = best
                self.level_scale, self.level_units, self.level_label = _level_info(ds[best])
                self.levels = [_nice(float(p) / self.level_scale) for p in ds[best].values.tolist()]
                skipped = [d for d in counts if d != best]
                if skipped:
                    log.info("%s: level axis %r (%d vars); variables on %s are not shown",
                             self.id, best, counts[best], ", ".join(f"{d!r} ({counts[d]})" for d in skipped))
            for v, (da, lev, ens) in fields.items():
                if lev and lev != self.level_dim:
                    continue
                if lev and self.level_chunk == 1:
                    try:
                        self.level_chunk = int(da.encoding.get("chunks", [1] * da.ndim)[list(da.dims).index(lev)])
                    except Exception:  # noqa: BLE001
                        pass
                if ens and not self.ensemble_dim:
                    self.ensemble_dim = ens
                if self.time_chunk == 1:
                    try:
                        self.time_chunk = int(da.encoding.get("chunks", [1])[list(da.dims).index("time")])
                    except Exception:  # noqa: BLE001
                        pass
                discovered[v] = (gkey, dict(da.attrs), da.dims)
        keep = _select_vars(list(discovered), cfg)
        if not keep:
            raise RuntimeError(f"{self.repo}: no displayable (time, y, x) variables")
        rng_start = min(e["start"] for e in self.experiments.values())
        rng_end = max(e["end"] for e in self.experiments.values())
        for v in keep:
            gkey, attrs, dims = discovered[v]
            self._var_group[v] = gkey
            self.variables[v] = _var_entry(v, attrs, dims, self.level_dim, gkey or self._realm(first_paths[gkey]),
                                           rng_start, rng_end, overrides.get(v))
        if self.ensemble_dim:
            ds = self._native(first_paths[self._var_group[keep[0]]])
            ids = [str(m) for m in ds[self.ensemble_dim].values]

            def _key(m):
                mm = re.match(r"r(\d+)", m)
                return int(mm.group(1)) if mm else m
            self.members = sorted(ids, key=_key)
        self.level_chunk = int(cfg.get("level_chunk", self.level_chunk))
        ds = self._native(first_paths[self._var_group[keep[0]]])
        self.calendar = str(ds.time.encoding.get("calendar", ds.time.attrs.get("calendar", "standard")))
        if not cfg.get("title"):
            self.title = str(ds.attrs.get("title") or self.id)
        self._loaded = True  # open() below needs the tables filled
        self.grid_text = cfg.get("grid") or _grid_text(self.open(first_exp, keep[0]), self.wrap)

    def _realm(self, path: str) -> str:
        a = self._native(path).attrs
        return str(a.get("modeling_realm") or a.get("realm")
                   or ("ocean" if any(k.startswith("cefi") for k in a) else ""))

    def _horizontal(self, ds: xr.Dataset):
        """(ydim, xdim, Regridder|None) for a dataset's horizontal grid."""
        if "lat" in ds.dims and "lon" in ds.dims and ds.lat.ndim == 1:
            return "lat", "lon", None
        if "latitude" in ds.dims and "longitude" in ds.dims:
            return "latitude", "longitude", None
        for la, lo in LATLON_2D:
            if la in ds.variables and lo in ds.variables and ds[la].ndim == 2:
                ydim, xdim = ds[la].dims
                key = hashlib.md5(f"{self.repo}:{self.branch}:{la}:{ds[la].shape}".encode()).hexdigest()[:12]
                cache = INDEX_DIR / f"{self.id}_{key}.npz"
                log.info("%s: curvilinear %s x %s grid — nearest-neighbour regrid index%s",
                         self.id, ds[la].shape[0], ds[la].shape[1], " (cached)" if cache.exists() else " (building)")
                return ydim, xdim, Regridder(ds[la].values, ds[lo].values, cache)
        return None

    # -- access
    def open(self, experiment, var):
        self.ensure()
        if experiment not in self.experiments:
            raise KeyError(experiment)
        self.var_cfg(var)
        gkey = self._var_group[var]
        path = self._exp_path[experiment][gkey]
        if self.regridder is None:
            ds = self._native(path)
            if "latitude" in ds.dims:
                ds = ds.rename({"latitude": "lat", "longitude": "lon"})
            return _normalise_grid(ds)
        key = f"{path}::{var}"
        with self._lock:
            if key not in self._regridded:
                # one variable per view, built on first use (a few seconds each)
                ds = self._native(path)
                ydim, xdim = self._hdims
                da = ds[var]
                ren = {d: self._dim_alias[d] for d in da.dims if d in self._dim_alias}
                if ren:  # e.g. sea-ice fields on yT/xT: same cells as yh/xh
                    da = da.rename(ren).drop_vars(list(ren), errors="ignore")
                self._regridded[key] = xr.Dataset({var: self.regridder.apply(da, ydim, xdim)},
                                                  attrs=dict(ds.attrs))
            return self._regridded[key]

    def read_block(self, experiment, var, t0, t1, stat, member, lev):
        if self.regridder is None:
            return super().read_block(experiment, var, t0, t1, stat, member, lev)
        # Read the native chunks for this time slab and level as a plain array
        # (dask only slices chunks here: no vectorised gather in the graph),
        # then regrid with the index table in NumPy.
        ds = self._native(self._exp_path[experiment][self._var_group[var]])
        da = ds[var]
        ren = {d: self._dim_alias[d] for d in da.dims if d in self._dim_alias}
        if ren:
            da = da.rename(ren).drop_vars(list(ren), errors="ignore")
        blk = self.level_select(self.reduce(da.isel(time=slice(t0, t1)), stat, member), lev)
        native = _retry(lambda: blk.values)
        return self.regridder.gather(native)

    # Original store attributes worth carrying into downloads. Member-specific
    # keys (realization, tracking_id, per-file history, creation_date) are
    # deliberately omitted: the group's copies describe whichever single run
    # seeded the Zarr consolidation, which is generally NOT the member subset.
    MODEL_LEVEL_KEYS = [
        "Conventions", "institution", "institute_id", "model_id", "modeling_realm",
        "product", "project_id", "source", "references", "license", "contact",
        "table_id", "initialization_method", "physics_version", "external_variables",
        "title", "cefi_experiment_name", "cefi_experiment_type", "cefi_region", "cefi_grid_type",
        "cefi_data_doi", "cefi_archive_version", "cefi_date_range",
    ]
    EXPERIMENT_LEVEL_KEYS = [
        "experiment", "experiment_id", "forcing", "branch_method", "parent_experiment_id",
    ]

    def download_attrs(self, experiment, var, include_experiment=True):
        ds = self._native(self._exp_path[experiment][self._var_group[var]])
        keys = self.MODEL_LEVEL_KEYS + (self.EXPERIMENT_LEVEL_KEYS if include_experiment else [])
        out = {k: ds.attrs[k] for k in keys if k in ds.attrs}
        if self.regridder is not None:
            out["regridding"] = ("nearest-neighbour from the native curvilinear grid to a regular "
                                 f"{self.regridder.lat.size} x {self.regridder.lon.size} lat/lon grid by the viewer")
        return out

    def experiment_attrs(self, experiment: str) -> dict:
        path = next(iter(self._exp_path[experiment].values()))
        return dict(self._native(path).attrs)


# ---------------------------------------------------------------- local catalog
class CatalogSource(Source):
    """A store built by ``tools/nc_catalog.py``: ``<path>/store`` (Icechunk,
    one group per variable, virtual chunks into the .nc files) and
    ``<path>/catalog.json``.

    Config::
        {"id": "ecda", "type": "catalog", "path": "/path/to/catalog",
         "title": "...", "variables": {"t_surf": {"label": "Surface temperature"}}}   # overrides optional
    The whole catalog is one "experiment" named after the dataset id.
    """

    kind = "catalog"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.path = Path(cfg["path"])
        if not self.path.is_absolute():
            self.path = (REPO_DIR / self.path).resolve()
        self.catalog = json.loads((self.path / "catalog.json").read_text())
        self._repo = None
        self._datasets: dict[str, xr.Dataset] = {}
        self.title = cfg.get("title") or self.catalog.get("title") or self.id
        self.description = cfg.get("description") or self.catalog.get("description", "")
        self.store_text = f"icechunk://{self.path / 'store'} (virtual refs into {', '.join(self.catalog['roots'])})"

    def _store(self):
        import icechunk as ic
        with self._lock:
            if self._repo is None:
                prefixes = self.catalog.get("virtual_chunk_containers") or [self.catalog["virtual_chunk_container"]]
                self._repo = ic.Repository.open(
                    ic.local_filesystem_storage(str(self.path / "store")),
                    authorize_virtual_chunk_access={p: ic.Credentials.LocalFileSystemAccess() for p in prefixes},
                )
            return self._repo.readonly_session("main").store

    def _group(self, var: str) -> xr.Dataset:
        with self._lock:
            if var not in self._datasets:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    ds = xr.open_zarr(self._store(), group=var, consolidated=False, decode_timedelta=False)
                ds = _normalise_grid(ds)
                self._datasets[var] = ds
            return self._datasets[var]

    def _load(self):
        cat = self.catalog
        overrides = self.cfg.get("variables") or {}
        starts, ends = [], []
        for name, v in cat["variables"].items():
            lev = v.get("level_name")
            if lev and not self.level_dim:
                self.level_dim = lev
                lu = _norm_units(v.get("level_units") or "hPa")
                self.level_scale, self.level_units, self.level_label = (
                    (100.0, "hPa", "Pressure level") if lu == "pa" else
                    (1.0, "hPa", "Pressure level") if lu in ("hpa", "mb") else
                    (1.0, v.get("level_units") or "", "Level"))
                self.levels = [_nice(float(p) / self.level_scale) for p in (v.get("levels") or [])]
            extra = [d for d in v["dims"] if d not in AUX_DIMS and d not in LEVEL_DIMS]
            if extra and not self.ensemble_dim:
                self.ensemble_dim = extra[0]
            self.variables[name] = _var_entry(name, v.get("attrs", {}), v["dims"], lev, v.get("realm") or "",
                                              v["start"], v["end"], overrides.get(name))
            self.variables[name]["missing_months"] = v.get("missing_months", [])
            starts.append(v["start"])
            ends.append(v["end"])
            if v.get("calendar"):
                self.calendar = str(v["calendar"]).lower()
        if self.ensemble_dim:
            first = next(iter(cat["variables"]))
            self.members = [str(m) for m in self._group(first)[self.ensemble_dim].values]
        g = cat.get("grid") or {}
        self.wrap = bool((g.get("lon_max", 360) - g.get("lon_min", 0)) + (g.get("dlon") or 0) >= 359.5)
        self.grid_text = (f"{g.get('nlat')} lat x {g.get('nlon')} lon"
                          + (f" ({g['dlat']:.3g} x {g['dlon']:.3g} deg)" if g.get("dlat") and g.get("dlon") else "")
                          + (", longitudes 0-360" if self.wrap else ", regional"))
        exp_id = self.cfg.get("experiment_id", self.id)
        self.experiments = {exp_id: {"start": min(starts), "end": max(ends), "short": self.title,
                                     "long": self.title + (f" — {self.description}" if self.description else "")}}

    def open(self, experiment, var):
        self.ensure()
        if experiment not in self.experiments:
            raise KeyError(experiment)
        self.var_cfg(var)
        return self._group(var)

    def download_attrs(self, experiment, var, include_experiment=True):
        g = dict(self.catalog.get("global_attrs") or {})
        g.pop("filename", None)  # per-file
        if "history" in g:       # keep the files' own history; the viewer writes its own
            g["source_history"] = g.pop("history")
        g["source_files"] = f"{self.catalog['variables'][var]['n_files']} monthly NetCDF files under {', '.join(self.catalog['roots'])}"
        return g


def _normalise_grid(ds: xr.Dataset) -> xr.Dataset:
    """Ascending lat, 0..360 lon (unless the domain straddles 0°),
    'lat'/'lon' names — what the endpoints assume."""
    ren = {}
    for a, b in (("latitude", "lat"), ("longitude", "lon")):
        if a in ds.dims and b not in ds.dims:
            ren[a] = b
    if ren:
        ds = ds.rename(ren)
    if ds.lat.size > 1 and float(ds.lat[0]) > float(ds.lat[-1]):
        ds = ds.isel(lat=slice(None, None, -1))
    lon = ds.lon.values.astype(float)
    straddles_zero = lon.min() < 0 <= lon.max() and (lon.max() - lon.min()) < 300
    if lon.min() < 0 and not straddles_zero:
        ds = ds.assign_coords(lon=(lon % 360)).sortby("lon")
    return ds


def _grid_text(ds: xr.Dataset, wrap: bool = True) -> str:
    lat = ds.lat.values.astype(float)
    lon = ds.lon.values.astype(float)
    dlat = abs(lat[1] - lat[0]) if lat.size > 1 else 0
    dlon = abs(lon[1] - lon[0]) if lon.size > 1 else 0
    span = ("longitudes 0-360" if wrap
            else f"regional: lat {lat.min():.2f}..{lat.max():.2f}, lon {((lon.min() + 180) % 360) - 180:.2f}..{((lon.max() + 180) % 360) - 180:.2f}")
    return f"{lat.size} lat x {lon.size} lon ({dlat:.3g} x {dlon:.3g} deg), {span}"


# ---------------------------------------------------------------- registry
# The SPEAR large ensemble as shipped in the original viewer. Curated labels
# and display units (the store's psl is already hPa, hence no rule-based
# conversion there) — everything else is discovered from the store.
SPEAR_DEFAULT = {
    "id": "spear",
    "type": "arraylake",
    "repo": "GFDL/noaa-gfdl-spear-large-ensembles-pds",
    "branch": "main",
    "title": "SPEAR-MED",
    "subtitle": "GFDL large ensemble · v31",
    "home": "https://www.gfdl.noaa.gov/spear/",
    "link": "https://noaa-gfdl-spear-large-ensembles-pds.s3.amazonaws.com/index.html"
            "#SPEAR/GFDL-LARGE-ENSEMBLES/CMIP/NOAA-GFDL/GFDL-SPEAR-MED/",
    "description": "NOAA GFDL SPEAR-MED large ensemble (30 members), monthly means, "
                   "0.5°x0.625° atmosphere (GFDL AM4.0) and 1° ocean (MOM6)",
    "grid": "0.5 deg lat x 0.625 deg lon (gr3), longitudes 0-360",
    "experiments": {
        "historical": {"start": "1921-01", "end": "2014-12", "short": "Historical"},
        "scenarioSSP5-85": {"start": "2015-01", "end": "2100-12", "short": "SSP5-8.5"},
    },
    "groups": ["Amon", "Omon"],
    "level_chunk": 9,
    "variables": {
        "pr": {"label": "Precipitation", "long": "Precipitation"},
        "tas": {"label": "Temperature (2 m)", "long": "Near-Surface Air Temperature (2 m height)"},
        "psl": {"label": "Sea-level pressure", "long": "Sea Level Pressure",
                "scale": 1.0, "offset": 0.0, "units": "hPa", "family": "pressure"},
        "sfcWind": {"label": "Wind speed (10 m)", "long": "Near-Surface Wind Speed (10 m height)"},
        "uas": {"label": "Zonal wind (10 m)", "long": "Eastward Near-Surface Wind (10 m height)"},
        "vas": {"label": "Meridional wind (10 m)", "long": "Northward Near-Surface Wind (10 m height)"},
        "rlut": {"label": "OLR (TOA)", "long": "TOA Outgoing Longwave Radiation"},
        "rsut": {"label": "Reflected SW (TOA)", "long": "TOA Outgoing Shortwave Radiation"},
        "rsdt": {"label": "Incoming solar (TOA)", "long": "TOA Incident Shortwave Radiation"},
        "ta": {"label": "Air temperature", "long": "Air Temperature (on pressure level)"},
        "ua": {"label": "Zonal wind", "long": "Eastward Wind (on pressure level)"},
        "va": {"label": "Meridional wind", "long": "Northward Wind (on pressure level)"},
        "zg": {"label": "Geopot. height", "long": "Geopotential Height (on pressure level)"},
        "hus": {"label": "Specific humidity", "long": "Specific Humidity (on pressure level)"},
        "tos": {"label": "SST", "long": "Sea Surface Temperature (ocean, 1° grid)"},
    },
    "chat_notes": (
        "The dataset is the NOAA GFDL SPEAR-MED large ensemble (30 members, experiments: "
        "historical 1921-2014 and SSP5-8.5 2015-2100, monthly means, 0.5°x0.625° atmosphere). "
        "If the user asks where to find or download the SPEAR data itself, point them to the "
        "public AWS S3 bucket: https://noaa-gfdl-spear-large-ensembles-pds.s3.amazonaws.com/"
        "index.html#SPEAR/GFDL-LARGE-ENSEMBLES/CMIP/NOAA-GFDL/GFDL-SPEAR-MED/ . The core SPEAR "
        "reference is Delworth et al. 2020, 'SPEAR: The Next Generation GFDL Modeling System for "
        "Seasonal to Multidecadal Prediction and Projection', J. Adv. Model. Earth Syst., "
        "[doi:10.1029/2019MS001895](https://doi.org/10.1029/2019MS001895)."
    ),
}

_KINDS = {"arraylake": ArrayLakeSource, "catalog": CatalogSource}


def load_sources() -> dict[str, Source]:
    """Read the datasets config (VIEWER_DATASETS or ./datasets.json); fall
    back to the built-in SPEAR definition. Sources that fail to construct
    (missing catalog dir, ...) are logged and skipped, never fatal."""
    path = os.environ.get("VIEWER_DATASETS") or str(REPO_DIR / "datasets.json")
    entries = None
    if Path(path).exists():
        try:
            entries = json.loads(Path(path).read_text()).get("datasets")
        except Exception as e:  # noqa: BLE001
            log.error("cannot read %s: %s", path, e)
    if not entries:
        entries = [SPEAR_DEFAULT]
    sources: dict[str, Source] = {}
    for e in entries:
        if e.get("disabled"):
            continue
        if e.get("type") == "arraylake" and e.get("repo") == SPEAR_DEFAULT["repo"] and "variables" not in e:
            e = {**SPEAR_DEFAULT, **e}  # let a bare SPEAR entry inherit the curated table
        cls = _KINDS.get(e.get("type"))
        if cls is None:
            log.error("dataset %r: unknown type %r", e.get("id"), e.get("type"))
            continue
        try:
            sources[e["id"]] = cls(e)
        except Exception as ex:  # noqa: BLE001
            log.error("dataset %r skipped: %s", e.get("id"), ex)
    if not sources:
        raise RuntimeError("no usable datasets configured")
    return sources
