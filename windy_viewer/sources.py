"""Data sources for the viewer.

A *source* is one dataset the viewer can display. Two kinds exist:

* ``ArrayLakeSource`` — an ArrayLake/Icechunk repo (e.g. the public SPEAR
  large ensemble), organised as ``<experiment>/<group>`` Zarr groups with an
  ensemble dimension and pressure levels.
* ``CatalogSource`` — a local Icechunk store built by ``tools/nc_catalog.py``
  from a directory of NetCDF files (virtual chunk references into the
  files: nothing is copied). One Zarr group per variable, no ensemble.

Both expose the same description (experiments, variables, members, levels,
wind pairs, display-unit conversion) and the same ``open(experiment, var)``
returning an xarray Dataset whose ``var`` has dims
``(time[, <ensemble>][, <level>], lat, lon)`` with ascending lat and
0..360 lon — everything server.py needs, so the endpoints never care which
kind they are talking to.

Sources are configured in a JSON file (``VIEWER_DATASETS``, default
``datasets.json`` next to the repo root); without one the built-in SPEAR
definition is used so the viewer behaves exactly as before.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
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
    (r"^(deg_?c|celsius|degrees?_?c(elsius)?|°c)$", 1.0, 0.0, "°C", "temp"),
    (r"^(kg/?m\^?-?2/?s\^?-?1|kgm-2s-1|kg/m2/s|kg/m\^2/s)$", 86400.0, 0.0, "mm/day", "precip"),
    (r"^(mm/day|mm/d|mmday-1)$", 1.0, 0.0, "mm/day", "precip"),
    (r"^(mm/hr|mm/h)$", 24.0, 0.0, "mm/day", "precip"),
    (r"^pa$", 0.01, 0.0, "hPa", "pressure"),
    (r"^(hpa|mb|mbar|millibar)$", 1.0, 0.0, "hPa", "pressure"),
    (r"^(m/s|ms-1|ms\^-1|m/sec|meters?/second)$", 1.0, 0.0, "m/s", "wind"),
    (r"^(w/?m\^?-?2|wm-2|w/m2|w/m\^2)$", 1.0, 0.0, "W/m²", "radiation"),
    (r"^(m|meters?|metres?)$", 1.0, 0.0, "m", "length"),
    (r"^(kg/kg|kgkg-1|1|none|dimensionless|fraction)$", 1.0, 0.0, "kg/kg", "ratio"),
    (r"^(%|percent)$", 1.0, 0.0, "%", "percent"),
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
WIND_PAIRS = [("uas", "vas"), ("u_ref", "v_ref"), ("u10", "v10"), ("ua", "va"),
              ("ucomp", "vcomp"), ("u", "v")]

AUX_DIMS = {"time", "lat", "lon", "latitude", "longitude", "bnds", "nv", "x", "y"}
LEVEL_DIMS = ("plev", "pfull", "level", "lev", "pressure", "p")


def _retry(fn, attempts=3):
    """Call fn() with retries — transient network stalls to S3 otherwise
    fail the request."""
    import time as _t
    last = None
    for k in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            _t.sleep(1.5 * (k + 1))
    raise last


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
        self.levels: list[int] = []           # hPa
        self.ensemble_dim: str | None = None
        self.level_dim: str | None = None
        self.level_scale: float = 1.0         # native level units -> hPa
        self.level_chunk: int = 1             # levels per chunk in the store
        self.wind: dict = {"sfc": None, "plev": None}
        self.calendar: str = "standard"
        self.grid_text: str = ""
        self.store_text: str = ""
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
            "members": self.members,
            "ensemble": bool(self.ensemble_dim),
            "n_members": len(self.members),
            "wind": {k: list(v) if v else None for k, v in self.wind.items()},
            "calendar": self.calendar,
            "grid": self.grid_text,
        }

    # -- data access
    def open(self, experiment: str, var: str) -> xr.Dataset:
        """Lazy dataset containing `var` (and lat/lon/time coords)."""
        raise NotImplementedError

    def download_attrs(self, experiment: str, var: str, include_experiment: bool = True) -> dict:
        """Global attributes to carry into NetCDF/CSV downloads."""
        return {}

    # -- helpers shared by the endpoints
    def var_cfg(self, var: str) -> dict:
        self.ensure()
        cfg = self.variables.get(var)
        if cfg is None:
            raise KeyError(var)
        return cfg

    def nearest_level(self, plev_hpa: float | None) -> int | None:
        """The available level (hPa) closest to the request; 500 hPa (or the
        middle level) when none is given."""
        if not self.levels:
            return None
        if plev_hpa is None:
            return 500 if 500 in self.levels else self.levels[len(self.levels) // 2]
        return min(self.levels, key=lambda p: abs(p - plev_hpa))

    def level_select(self, da: xr.DataArray, plev_hpa: float | None) -> xr.DataArray:
        """Select one level (given in hPa) on a level-bearing array."""
        if not self.level_dim or self.level_dim not in da.dims:
            return da
        target = self.nearest_level(plev_hpa) * self.level_scale
        return da.sel({self.level_dim: target}, method="nearest")

    def experiment_attrs(self, experiment: str) -> dict:
        return {}

    def level_hpa(self, da: xr.DataArray) -> int | None:
        if self.level_dim and self.level_dim in da.coords:
            return int(round(float(da[self.level_dim].values) / self.level_scale))
        return None

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


# ---------------------------------------------------------------- ArrayLake
class ArrayLakeSource(Source):
    """``<experiment>/<group>`` Zarr groups in an ArrayLake repo.

    Config::
        {"id": "spear", "type": "arraylake",
         "repo": "GFDL/noaa-gfdl-spear-large-ensembles-pds", "branch": "main",
         "experiments": {"historical": {"start": "1921-01", "end": "2014-12"}, ...},
         "groups": ["Amon", "Omon"],              # Zarr groups to scan
         "variables": {"pr": {"label": ..., "group": "Amon", ...}, ...}   # optional overrides;
                                                  # omit to take every lat/lon variable found
         "level_chunk": 9}
    """

    kind = "arraylake"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.repo = cfg["repo"]
        self.branch = cfg.get("branch", "main")
        self.groups = cfg.get("groups", ["Amon"])
        self._session = None
        self._datasets: dict[str, xr.Dataset] = {}
        self.store_text = f"arraylake://{self.repo} (branch {self.branch})"

    def _group(self, experiment: str, group: str) -> xr.Dataset:
        key = f"{experiment}/{group}"
        with self._lock:
            if key not in self._datasets:
                if self._session is None:
                    from arraylake import Client
                    repo = Client().get_repo(self.repo)
                    self._session = repo.readonly_session(branch=self.branch)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._datasets[key] = xr.open_zarr(self._session.store, group=key, consolidated=False)
            return self._datasets[key]

    def _load(self):
        self.experiments = {}
        exps = self.cfg.get("experiments") or {}
        overrides = self.cfg.get("variables")
        first_exp = next(iter(exps))
        for name, rng in exps.items():
            attrs = self._group(name, self.groups[0]).attrs
            desc = attrs.get("experiment", name)
            forcing = attrs.get("forcing", "")
            self.experiments[name] = {**rng, "long": f"{desc}" + (f" — forcing: {forcing}" if forcing else "")}
        # variables: every (time, lat, lon) data variable in the listed groups
        for group in self.groups:
            ds = self._group(first_exp, group)
            for v, da in ds.data_vars.items():
                if not {"lat", "lon"} <= set(da.dims) or "time" not in da.dims:
                    continue
                if overrides is not None and v not in overrides:
                    continue
                if self.ensemble_dim is None:
                    extra = [d for d in da.dims if d not in AUX_DIMS and d not in LEVEL_DIMS]
                    if extra:
                        self.ensemble_dim = extra[0]
                lev = next((d for d in da.dims if d in LEVEL_DIMS), None)
                if lev and not self.level_dim:
                    self.level_dim = lev
                    lu = _norm_units(ds[lev].attrs.get("units", "Pa"))
                    self.level_scale = 100.0 if lu == "pa" else 1.0
                    self.levels = [int(round(float(p) / self.level_scale)) for p in ds[lev].values.tolist()]
                    try:
                        self.level_chunk = int(da.chunksizes[lev][0])
                    except Exception:  # noqa: BLE001
                        self.level_chunk = 1
                rng = exps[first_exp]
                self.variables[v] = _var_entry(v, da.attrs, da.dims, lev, group,
                                               min(e["start"] for e in exps.values()),
                                               max(e["end"] for e in exps.values()),
                                               (overrides or {}).get(v))
        if overrides:  # keep the configured order
            self.variables = {k: self.variables[k] for k in overrides if k in self.variables}
        if self.ensemble_dim:
            ds = self._group(first_exp, self.groups[0])
            ids = [str(m) for m in ds[self.ensemble_dim].values]
            def _key(m):
                mm = re.match(r"r(\d+)", m)
                return int(mm.group(1)) if mm else m
            self.members = sorted(ids, key=_key)
        self.level_chunk = int(self.cfg.get("level_chunk", self.level_chunk))
        ds = self._group(first_exp, self.groups[0])
        self.calendar = str(ds.time.encoding.get("calendar", ds.time.attrs.get("calendar", "standard")))
        self.grid_text = self.cfg.get("grid") or _grid_text(ds)

    def open(self, experiment, var):
        self.ensure()
        if experiment not in self.experiments:
            raise KeyError(experiment)
        return self._group(experiment, self.var_cfg(var)["group"])

    # Original store attributes worth carrying into downloads. Member-specific
    # keys (realization, tracking_id, per-file history, creation_date) are
    # deliberately omitted: the group's copies describe whichever single run
    # seeded the Zarr consolidation, which is generally NOT the member subset.
    MODEL_LEVEL_KEYS = [
        "Conventions", "institution", "institute_id", "model_id", "modeling_realm",
        "product", "project_id", "source", "references", "license", "contact",
        "table_id", "initialization_method", "physics_version", "external_variables",
    ]
    EXPERIMENT_LEVEL_KEYS = [
        "experiment", "experiment_id", "forcing", "branch_method", "parent_experiment_id",
    ]

    def download_attrs(self, experiment, var, include_experiment=True):
        ds = self.open(experiment, var)
        keys = self.MODEL_LEVEL_KEYS + (self.EXPERIMENT_LEVEL_KEYS if include_experiment else [])
        return {k: ds.attrs[k] for k in keys if k in ds.attrs}

    def experiment_attrs(self, experiment: str) -> dict:
        return dict(self._group(experiment, self.groups[0]).attrs)


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
                self.level_scale = 100.0 if lu == "pa" else 1.0
                self.levels = [int(round(float(p) / self.level_scale)) for p in (v.get("levels") or [])]
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
        self.grid_text = (f"{g.get('nlat')} lat x {g.get('nlon')} lon"
                          + (f" ({g['dlat']:.3g} x {g['dlon']:.3g} deg)" if g.get("dlat") and g.get("dlon") else "")
                          + ", longitudes 0-360")
        exp_id = self.cfg.get("experiment_id", self.id)
        self.experiments = {exp_id: {"start": min(starts), "end": max(ends),
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
    """Ascending lat, 0..360 lon, 'lat'/'lon' names — what the endpoints assume."""
    ren = {}
    for a, b in (("latitude", "lat"), ("longitude", "lon")):
        if a in ds.dims and b not in ds.dims:
            ren[a] = b
    if ren:
        ds = ds.rename(ren)
    if ds.lat.size > 1 and float(ds.lat[0]) > float(ds.lat[-1]):
        ds = ds.isel(lat=slice(None, None, -1))
    lon = ds.lon.values.astype(float)
    if lon.min() < 0:
        ds = ds.assign_coords(lon=(lon % 360)).sortby("lon")
    return ds


def _grid_text(ds: xr.Dataset) -> str:
    lat = ds.lat.values.astype(float)
    lon = ds.lon.values.astype(float)
    dlat = abs(lat[1] - lat[0]) if lat.size > 1 else 0
    dlon = abs(lon[1] - lon[0]) if lon.size > 1 else 0
    return f"{lat.size} lat x {lon.size} lon ({dlat:.3g} x {dlon:.3g} deg), longitudes 0-360"


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
