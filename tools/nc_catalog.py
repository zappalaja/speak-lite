#!/usr/bin/env python
"""Build a virtual Icechunk catalog from a directory tree of NetCDF files.

The viewer reads Zarr/Icechunk stores. For data that lives as plain NetCDF
files on a local or HPC filesystem this tool builds an Icechunk repository
whose chunks are *virtual references* into the original files (VirtualiZarr):
nothing is copied, and the viewer reads each month's chunk straight from
the file it came from, exactly as it reads ArrayLake-hosted chunks.

    python tools/nc_catalog.py check /data/.../ecda_reanalysis/mo
    python tools/nc_catalog.py build /data/.../ecda_reanalysis/mo --out ~/catalogs/ecda \\
        --id ecda --title "ECDA reanalysis"
    python tools/nc_catalog.py update --out ~/catalogs/ecda      # pick up new months
    python tools/nc_catalog.py info ~/catalogs/ecda

Or drive everything from a config file (source dirs -> catalogs under one
output root; existing catalogs are updated, new ones built):

    python tools/nc_catalog.py example-config > catalogs.json   # edit it
    python tools/nc_catalog.py run --config catalogs.json       # all catalogs
    python tools/nc_catalog.py run --config catalogs.json ecda --check-only

Any directory tree of NetCDF files works: every file's header is read
and the data variables (anything on a time x lat x lon grid, optionally
with a level dimension), the time axis and its calendar come from the
file itself. Files of one variable are stitched together in time order,
whether that is one month per file (FMS: atmos.YYYYMM-YYYYMM.var.nc),
thirty years of daily steps in one file (CMIP: var_day_..._YYYYMMDD-
YYYYMMDD.nc) or several variables per file. The time frequency (year,
month, day, 12hr, 6hr, 3hr, 1hr) is detected per variable and recorded
in catalog.json; --frequency forces it. Filename patterns are optional
hints (--pattern, --var-from) and only needed for odd naming.

Every file is inspected first (format, grid, time axis, units, calendar,
fill values) and problems are logged as WARNING (catalog still built,
file kept) or ERROR (file skipped). The store is only written after all
checks pass, or with --keep-going. The build writes:
    <out>/store/        Icechunk repository (virtual chunk refs -> the .nc files)
    <out>/catalog.json  what the viewer needs: variables, time ranges, grid, attrs, checks
    <out>/catalog.log   full log of the build
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger("nc_catalog")

# Icechunk's Rust logger warns on every local-filesystem commit/open
# ("not safe for concurrent commits"); one process writes here, so keep
# only errors unless the user set their own level.
os.environ.setdefault("ICECHUNK_LOG", "error")


# ---------------------------------------------------------------- progress
# On a terminal: one line updated in place (count, %, elapsed, ETA, current
# file). Redirected (batch job, log file): a log line every 10%. Log
# records emitted while a bar is active are printed above it.
_active_progress = None


class Progress:
    def __init__(self, total: int, label: str):
        self.total = max(1, total)
        self.label = label
        self.n = 0
        self.t0 = time.time()
        self.tty = sys.stderr.isatty()
        self.next_pct = 10
        self.last_log = self.t0
        self.msg = ""
        global _active_progress
        _active_progress = self

    def _line(self) -> str:
        el = time.time() - self.t0
        eta = el / self.n * (self.total - self.n) if self.n else 0
        pct = 100 * self.n // self.total
        bar = "#" * (pct // 5) + "." * (20 - pct // 5)
        line = f"  {self.label} [{bar}] {self.n}/{self.total} {pct:3d}%  {el:5.0f}s  ETA {eta:4.0f}s  {self.msg}"
        return line[: shutil.get_terminal_size((100, 20)).columns - 1]

    def draw(self):
        if self.tty:
            sys.stderr.write("\r\x1b[2K" + self._line())
            sys.stderr.flush()

    def clear(self):
        if self.tty:
            sys.stderr.write("\r\x1b[2K")
            sys.stderr.flush()

    def step(self, msg: str = ""):
        self.n += 1
        self.msg = msg
        pct = 100 * self.n // self.total
        if self.tty:
            self.draw()
        elif pct >= self.next_pct and time.time() - self.last_log >= 5:
            log.info("%s: %d/%d (%d%%) %.0fs elapsed", self.label, self.n, self.total, pct, time.time() - self.t0)
            self.next_pct = (pct // 10 + 1) * 10
            self.last_log = time.time()

    def done(self, note: str = ""):
        global _active_progress
        self.clear()
        _active_progress = None
        log.info("%s: %d in %.1fs%s", self.label, self.n, time.time() - self.t0, f" — {note}" if note else "")


class _ProgressAwareHandler(logging.StreamHandler):
    """Console handler that keeps the progress line at the bottom."""

    def emit(self, record):
        prog = _active_progress
        if prog is not None:
            prog.clear()
        super().emit(record)
        if prog is not None:
            prog.draw()

# Filename conventions the tool recognises on its own. They only provide
# *hints* (variable name, realm, date range); everything that matters is
# read from the file itself, so files with other names work too.
FMS_PATTERN = r"^(?P<realm>[^.]+)\.(?P<start>\d{6,10})-(?P<end>\d{6,10})\.(?P<var>.+)\.nc4?$"
CMIP_PATTERN = (r"^(?P<var>[A-Za-z0-9]+)_(?P<table>[A-Za-z0-9]+)_(?P<rest>.+?)_"
                r"(?P<start>\d{4,12})-(?P<end>\d{4,12})(?:-clim)?\.nc4?$")  # table (day, Amon) is not a realm
DEFAULT_PATTERN = None  # auto: try the known conventions, else content only
KNOWN_PATTERNS = [FMS_PATTERN, CMIP_PATTERN]
# any "YYYYMMDD-YYYYMMDD"-like pair somewhere in the name
_STAMP_RX = re.compile(r"(?<!\d)(\d{4}|\d{6}|\d{8}|\d{10}|\d{12})-(\d{4}|\d{6}|\d{8}|\d{10}|\d{12})(?!\d)")
NC_SUFFIXES = {".nc", ".nc4", ".cdf", ".netcdf"}

# Names that are coordinates/bounds/bookkeeping, never the data variable.
AUX_NAMES = {"time", "lat", "lon", "latitude", "longitude", "bnds", "nv", "plev", "pfull", "phalf",
             "level", "lev", "time_bnds", "lat_bnds", "lon_bnds", "average_T1", "average_T2",
             "average_DT", "lat_bounds", "lon_bounds", "time_bounds", "height", "member_id",
             "crs", "spatial_ref", "area", "areacella", "sftlf", "lon_vertices", "lat_vertices"}
LEVEL_NAMES = ("plev", "pfull", "phalf", "level", "lev", "height", "depth", "z", "pressure")
REALM_WORDS = {"atmos", "ocean", "land", "ice", "seaice", "aerosol", "atmoschem", "landice",
               "ocnbgchem", "atmosphere", "sea_ice"}

# Time frequencies the catalog can index. `days` is the nominal step; `fmt`
# the strftime-like period tag (sortable string); `label` the human form.
FREQS = {
    "year":  {"days": 365.25, "fmt": "%Y", "label": "%Y", "cmip": ("yr", "year", "annual", "ann")},
    "month": {"days": 30.44, "fmt": "%Y%m", "label": "%Y-%m", "cmip": ("mon", "month", "monthly")},
    "day":   {"days": 1.0, "fmt": "%Y%m%d", "label": "%Y-%m-%d", "cmip": ("day", "daily")},
    "12hr":  {"days": 0.5, "fmt": "%Y%m%d%H", "label": "%Y-%m-%dT%H", "cmip": ("12hr",)},
    "6hr":   {"days": 0.25, "fmt": "%Y%m%d%H", "label": "%Y-%m-%dT%H", "cmip": ("6hr", "6hourly")},
    "3hr":   {"days": 0.125, "fmt": "%Y%m%d%H", "label": "%Y-%m-%dT%H", "cmip": ("3hr",)},
    "1hr":   {"days": 1 / 24, "fmt": "%Y%m%d%H", "label": "%Y-%m-%dT%H", "cmip": ("1hr", "hr", "hourly")},
}


class CatalogError(Exception):
    pass


# ---------------------------------------------------------------- periods
def period_tag(t, freq: str) -> str:
    """Sortable tag of the period a time value falls in ('202005', '20200517', ...)."""
    fmt = FREQS[freq]["fmt"]
    out = f"{t.year:04d}"
    if "%m" in fmt:
        out += f"{t.month:02d}"
    if "%d" in fmt:
        out += f"{t.day:02d}"
    if "%H" in fmt:
        out += f"{t.hour:02d}"
    return out


def tag_label(tag: str, freq: str) -> str:
    """'20200517' -> '2020-05-17' (the form catalog.json and the viewer use)."""
    parts = [tag[:4]]
    if len(tag) >= 6:
        parts.append(tag[4:6])
    if len(tag) >= 8:
        parts.append(tag[6:8])
    out = "-".join(parts)
    if len(tag) >= 10:
        out += f"T{tag[8:10]}"
    return out


def month_tags(start: str, end: str) -> list[str]:
    """['202005', '202006', ...] inclusive."""
    y, m = int(start[:4]), int(start[4:6])
    y1, m1 = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def period_tags(first: str, last: str, freq: str, calendar: str) -> list[str]:
    """Every period tag from `first` to `last` inclusive, stepping in the
    file's calendar (so a noleap daily series has no Feb 29)."""
    if freq == "year":
        return [f"{y:04d}" for y in range(int(first[:4]), int(last[:4]) + 1)]
    if freq == "month":
        return month_tags(first, last)
    import cftime
    from datetime import timedelta
    cal = (calendar or "standard").lower()
    step = timedelta(days=FREQS[freq]["days"])
    y, m, d = int(first[:4]), int(first[4:6]), int(first[6:8])
    h = int(first[8:10]) if len(first) >= 10 else 0
    t = cftime.datetime(y, m, d, h, calendar=cal)
    out = []
    while True:
        tag = period_tag(t, freq)
        if tag > last:
            break
        out.append(tag)
        t = t + step
    return out


def _norm_freq(name) -> str | None:
    if not name:
        return None
    n = str(name).strip().lower()
    for k, f in FREQS.items():
        if n == k or n in f["cmip"]:
            return k
    return None


def _classify_step(days: float) -> str | None:
    if days <= 0:
        return None
    for k in ("1hr", "3hr", "6hr", "12hr", "day"):
        if abs(days - FREQS[k]["days"]) <= FREQS[k]["days"] * 0.15:
            return k
    if 27 <= days <= 32:
        return "month"
    if 359 <= days <= 367:
        return "year"
    return None


def detect_frequency(times, bounds_days: float | None, attr_hint, name_hint: str | None) -> tuple[str | None, str]:
    """(frequency, how) from the decoded time values; falls back to the
    averaging-period width, the 'frequency' global attribute, then the
    length of the date stamps in the filename."""
    if len(times) >= 2:
        deltas = []
        for a, b in zip(times[:-1], times[1:]):
            dt = b - a
            deltas.append(dt.days + dt.seconds / 86400.0)
        f = _classify_step(float(np.median(deltas)))
        if f:
            return f, "time step"
        return None, f"irregular time step (median {np.median(deltas):.3g} days)"
    if bounds_days:
        f = _classify_step(bounds_days)
        if f:
            return f, "averaging period"
    f = _norm_freq(attr_hint)
    if f:
        return f, "'frequency' attribute"
    if name_hint:
        f = {4: "year", 6: "month", 8: "day", 10: "6hr"}.get(len(name_hint))
        if f:
            return f, "filename date stamp"
    return None, "single time step and no other clue"


# ---------------------------------------------------------------- discovery
def _name_hints(name: str, pattern: str | None) -> dict | None:
    """Parse what the filename says (var, realm, start, end). With an explicit
    pattern a non-match returns None (file skipped); in auto mode the known
    conventions are tried and a bare date-stamp pair is the last resort."""
    if pattern:
        m = re.match(pattern, name)
        return m.groupdict() if m else None
    for rx in KNOWN_PATTERNS:
        m = re.match(rx, name)
        if m:
            d = m.groupdict()
            d.pop("rest", None)
            d.pop("table", None)
            return d
    m = _STAMP_RX.search(name)
    if m and len(m.group(1)) == len(m.group(2)):
        return {"start": m.group(1), "end": m.group(2)}
    return {}


def _realm_from_path(path: Path) -> str | None:
    for part in reversed(path.parent.parts):
        if part.lower() in REALM_WORDS:
            return part
    return None


def discover(roots: list[Path], pattern: str | None, var_from: str) -> dict[str, list[dict]]:
    """Walk the roots, read every file's header once, and group the files
    by data variable. Returns {var: [{"path", "info", "hints", "realm"}]}."""
    paths = []
    for root in roots:
        if not root.is_dir():
            raise CatalogError(f"not a directory: {root}")
        paths += sorted(p for p in root.rglob("*") if p.suffix.lower() in NC_SUFFIXES and p.is_file())
    groups: dict[str, list[dict]] = defaultdict(list)
    n_skipped = 0
    prog = Progress(len(paths), "scanning files")
    for p in paths:
        prog.step(p.name)
        hints = _name_hints(p.name, pattern)
        if hints is None:
            log.warning("SKIP %s: name does not match pattern %r", p, pattern)
            n_skipped += 1
            continue
        try:
            info = inspect_file(p)
        except CatalogError as e:
            log.error("SKIP %s: %s", p, e)
            n_skipped += 1
            continue
        except Exception as e:  # noqa: BLE001  truncated / corrupt / permission
            log.error("SKIP %s: unreadable (%s: %s)", p, type(e).__name__, str(e)[:120])
            n_skipped += 1
            continue
        if var_from == "dir":
            wanted = [p.parent.name]
        elif var_from == "filename":
            wanted = [hints.get("var")] if hints.get("var") else []
        else:  # auto: the filename's variable if the file has it, else every data variable
            hv = hints.get("var")
            wanted = [hv] if hv in info["vars"] else list(info["vars"])
        wanted = [v for v in wanted if v]
        if not wanted:
            log.warning("SKIP %s: no data variable with a time axis (has %s)", p, info["all_vars"])
            n_skipped += 1
            continue
        realm = (hints.get("realm") or info["global_attrs"].get("realm")
                 or info["global_attrs"].get("modeling_realm") or _realm_from_path(p) or None)
        for v in wanted:
            if v not in info["vars"]:
                log.warning("SKIP %s: variable %r (from %s) not in file, has %s", p, v, var_from, list(info["vars"]))
                n_skipped += 1
                continue
            groups[v].append({"path": p, "info": info, "hints": hints, "realm": realm})
    prog.done(f"{len(paths) - n_skipped} usable, {n_skipped} skipped, {len(groups)} variable(s)")
    log.info("discovered %d files under %s: %s", len(paths), ", ".join(str(r) for r in roots),
             ", ".join(f"{k} ({len(v)})" for k, v in groups.items()) or "nothing")
    return dict(groups)


# ---------------------------------------------------------------- inspection
def _find_coord(nc, axis: str, std: str, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in nc.variables and nc.variables[n].ndim == 1:
            return n
    for n, v in nc.variables.items():
        if v.ndim != 1:
            continue
        if str(getattr(v, "axis", "")).upper() == axis or str(getattr(v, "standard_name", "")).lower() == std:
            return n
    return None


def inspect_file(path: Path) -> dict:
    """Cheap header + coordinate read with netCDF4 (no data variable read).
    Describes every data variable in the file (those with a time axis and
    lat/lon dimensions)."""
    from netCDF4 import Dataset
    import cftime

    with Dataset(path, "r") as nc:
        if nc.data_model.startswith("NETCDF3"):
            # A truncated classic file opens fine and reads *silently* as
            # fill values, so check the size against the layout's lower bound.
            need = _nc3_min_size(nc)
            have = path.stat().st_size
            if have < need:
                raise CatalogError(f"truncated: {have} bytes on disk, data alone needs {need}")
        info = {"format": nc.data_model, "dims": {k: len(v) for k, v in nc.dimensions.items()},
                "unlimited": [k for k, v in nc.dimensions.items() if v.isunlimited()],
                "variables": list(nc.variables), "all_vars": list(nc.variables),
                "global_attrs": {k: _py(nc.getncattr(k)) for k in nc.ncattrs()}}
        lat_n = _find_coord(nc, "Y", "latitude", ("lat", "latitude", "y", "nav_lat"))
        lon_n = _find_coord(nc, "X", "longitude", ("lon", "longitude", "x", "nav_lon"))
        time_n = _find_coord(nc, "T", "time", ("time", "Time", "t"))
        if not lat_n or not lon_n:
            raise CatalogError(f"no latitude/longitude coordinates (variables: {list(nc.variables)})")
        if not time_n:
            raise CatalogError(f"no time coordinate (variables: {list(nc.variables)})")
        info["coord_names"] = {"lat": lat_n, "lon": lon_n, "time": time_n}
        for c, n in (("lat", lat_n), ("lon", lon_n)):
            info[c] = np.asarray(nc.variables[n][:], dtype=float)
            info[f"{c}_attrs"] = {k: _py(nc.variables[n].getncattr(k)) for k in nc.variables[n].ncattrs()}
        t = nc.variables[time_n]
        units = getattr(t, "units", None)
        cal = getattr(t, "calendar", getattr(t, "calendar_type", "standard"))
        if not units:
            raise CatalogError("time has no units attribute")
        raw = np.asarray(t[:], dtype=float)
        try:
            times = cftime.num2date(raw, units, calendar=cal.lower())
        except Exception as e:
            raise CatalogError(f"cannot decode time ({units!r}, calendar {cal!r}): {e}")
        info["time_units"], info["calendar"], info["times"] = units, cal, list(times)
        # width of the averaging period (days), when the file records it
        info["bounds_days"] = None
        try:
            if "average_DT" in nc.variables:
                info["bounds_days"] = float(np.asarray(nc.variables["average_DT"][:], dtype=float)[0])
            else:
                bn = getattr(t, "bounds", None)
                if bn and bn in nc.variables:
                    b = np.asarray(nc.variables[bn][:], dtype=float)
                    if b.ndim == 2 and b.shape[1] == 2:
                        bu = getattr(nc.variables[bn], "units", units)
                        if cftime and bu and "since" in str(bu):
                            b0, b1 = cftime.num2date(b[0], bu, calendar=cal.lower())
                            d = b1 - b0
                            info["bounds_days"] = d.days + d.seconds / 86400.0
                        else:
                            info["bounds_days"] = float(b[0, 1] - b[0, 0])
        except Exception:  # noqa: BLE001  bounds are a hint only
            pass
        tdim = t.dimensions[0] if t.dimensions else time_n
        info["vars"] = {}
        for name, v in nc.variables.items():
            if name in AUX_NAMES or name in (lat_n, lon_n, time_n) or v.ndim < 3:
                continue
            dims = v.dimensions
            if tdim not in dims or nc.variables[lat_n].dimensions[0] not in dims \
                    or nc.variables[lon_n].dimensions[0] not in dims:
                continue
            if str(getattr(v, "standard_name", "")).endswith("_bounds") or name.endswith(("_bnds", "_bounds")):
                continue
            entry = {"dtype": str(v.dtype), "dims": tuple(dims),
                     "attrs": {k: _py(v.getncattr(k)) for k in v.ncattrs()}}
            for c in LEVEL_NAMES:
                if c in nc.variables and c in dims and nc.variables[c].ndim == 1:
                    entry["level_name"] = c
                    entry["levels"] = np.asarray(nc.variables[c][:], dtype=float)
                    entry["level_attrs"] = {k: _py(nc.variables[c].getncattr(k)) for k in nc.variables[c].ncattrs()}
                    break
            info["vars"][name] = entry
        info["data_vars"] = list(info["vars"])
    return info


def _nc3_min_size(nc) -> int:
    """Lower bound on a classic-format file's size: every fixed variable's
    data (4-byte aligned) plus one record's worth of the record variables
    per record. Excludes the header, so a smaller file is truncated."""
    unlimited = [k for k, d in nc.dimensions.items() if d.isunlimited()]
    nrec = len(nc.dimensions[unlimited[0]]) if unlimited else 0
    fixed = rec = 0
    for v in nc.variables.values():
        dims = v.dimensions
        n = int(np.prod([len(nc.dimensions[d]) for d in dims if d not in unlimited], dtype=np.int64))
        size = n * v.dtype.itemsize
        size += (-size) % 4
        if dims and dims[0] in unlimited:
            rec += size
        else:
            fixed += size
    return fixed + nrec * rec


def _py(v):
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _hint_tags(hints: dict, freq: str) -> tuple[str, str] | None:
    """The period range the filename claims, at the catalog's precision.
    A stamp coarser than the frequency (monthly name, daily data) covers
    the whole month; finer stamps are truncated."""
    s, e = hints.get("start"), hints.get("end")
    if not (s and e and s.isdigit() and e.isdigit()):
        return None
    n = len(period_tag(datetime(2000, 1, 1), freq))
    first, last = s, e
    if len(first) < 6:
        first += "01"
    if len(first) < 8:
        first += "01"
    if len(first) < 10:
        first += "00"
    if len(last) < 6:
        last += "12"
    if len(last) < 8:
        import calendar as _cal
        last += f"{_cal.monthrange(int(last[:4]), int(last[4:6]))[1]:02d}"  # gregorian upper bound; a hint only
    if len(last) < 10:
        last += "23"
    return first[:n], last[:n]


def check_variable(var: str, files: list[dict], force_freq: str | None = None) -> dict:
    """Inspect every file of one variable; drop broken ones; return the
    consolidated description plus the list of problems."""
    files.sort(key=lambda f: (str(f["info"]["times"][0]) if f["info"]["times"] else "", f["path"].name))
    ref = None
    ref_var = None
    freq = force_freq
    keep = []
    problems = []  # (level, message)

    def warn(msg):
        problems.append(("WARNING", msg))
        log.warning("%s: %s", var, msg)

    def err(msg):
        problems.append(("ERROR", msg))
        log.error("%s: %s", var, msg)

    seen: dict[str, Path] = {}
    prog = Progress(len(files), f"{var}: checking")
    for f in files:
        p = f["path"]
        info = f["info"]
        vinfo = info["vars"][var]
        prog.step(p.name)
        if not info["times"]:
            err(f"{p.name}: empty time axis -- file skipped")
            continue
        f_freq, how = detect_frequency(info["times"], info.get("bounds_days"),
                                       info["global_attrs"].get("frequency"), f["hints"].get("start"))
        if freq is None:
            if f_freq is None:
                err(f"{p.name}: cannot tell the time frequency ({how}); pass --frequency -- file skipped")
                continue
            freq = f_freq
            log.info("%s: %s data (from the %s of %s)", var, freq, how, p.name)
        elif f_freq is not None and f_freq != freq and not force_freq:
            err(f"{p.name}: {f_freq} data but the catalog is {freq} -- file skipped")
            continue
        tags = [period_tag(t, freq) for t in info["times"]]
        if len(set(tags)) != len(tags):
            err(f"{p.name}: several time steps fall in the same {freq} period -- file skipped")
            continue
        hint = _hint_tags(f["hints"], freq)
        if hint:
            outside = [t for t in tags if not (hint[0] <= t <= hint[1])]
            if outside:
                err(f"{p.name}: time {tag_label(outside[0], freq)} is outside the name's "
                    f"{f['hints']['start']}-{f['hints']['end']} -- file skipped")
                continue
        dup = next((t for t in tags if t in seen), None)
        if dup:
            err(f"{p.name}: {tag_label(dup, freq)} already provided by {seen[dup].name} -- file skipped")
            continue
        if ref is not None:
            bad = False
            for c in ("lat", "lon"):
                if info[c].shape != ref[c].shape or not np.allclose(info[c], ref[c], atol=1e-6):
                    err(f"{p.name}: {c} grid differs from {keep[0]['path'].name} "
                        f"({info[c].shape} vs {ref[c].shape}) -- file skipped")
                    bad = True
            if vinfo["dims"] != ref_var["dims"]:
                err(f"{p.name}: dimensions {vinfo['dims']} differ from {ref_var['dims']} -- file skipped")
                bad = True
            if "levels" in ref_var and (vinfo.get("levels") is None or vinfo["levels"].shape != ref_var["levels"].shape
                                        or not np.allclose(vinfo["levels"], ref_var["levels"])):
                err(f"{p.name}: vertical levels differ -- file skipped")
                bad = True
            if bad:
                continue
            if info["format"] != ref["format"]:
                warn(f"{p.name}: format {info['format']} differs from {ref['format']} "
                     "(mixed formats work but need both parsers)")
            if vinfo["dtype"] != ref_var["dtype"]:
                warn(f"{p.name}: dtype {vinfo['dtype']} differs from {ref_var['dtype']}")
            for a in ("units", "long_name", "_FillValue", "missing_value"):
                if vinfo["attrs"].get(a) != ref_var["attrs"].get(a):
                    warn(f"{p.name}: {var}:{a} = {vinfo['attrs'].get(a)!r} differs from "
                         f"{ref_var['attrs'].get(a)!r} in {keep[0]['path'].name}")
            if info["calendar"].lower() != ref["calendar"].lower() or info["time_units"] != ref["time_units"]:
                warn(f"{p.name}: time encoding ({info['time_units']!r}, {info['calendar']}) differs "
                     f"from ({ref['time_units']!r}, {ref['calendar']})")
        else:
            ref, ref_var = info, vinfo
        for t in tags:
            seen[t] = p
        f["tags"] = tags
        keep.append(f)
    prog.done(f"{len(keep)} usable, {len(files) - len(keep)} skipped")

    if not keep:
        err("no usable files")
        return {"var": var, "files": [], "problems": problems, "freq": freq}

    tags = sorted(t for f in keep for t in f["tags"])
    full = period_tags(tags[0], tags[-1], freq, ref["calendar"])
    missing = sorted(set(full) - set(tags))
    if missing:
        warn(f"{len(missing)} missing {freq} period(s) in {tag_label(tags[0], freq)}..{tag_label(tags[-1], freq)}: "
             + ", ".join(tag_label(m, freq) for m in missing[:12]) + (" ..." if len(missing) > 12 else ""))
    if "units" not in ref_var["attrs"]:
        warn("no units attribute on the data variable")
    if not ref_var["attrs"].get("long_name"):
        warn("no long_name attribute on the data variable")
    for c in ("lat", "lon"):
        u = str(ref[f"{c}_attrs"].get("units", "")).lower()
        if not (u.startswith("degree")):
            warn(f"{c} units {u!r} do not look like degrees")
    lat = ref["lat"]
    if lat[0] > lat[-1]:
        warn("latitude runs north-to-south; the viewer expects ascending latitudes (it will flip)")
    lon = ref["lon"]
    if lon.min() < 0:
        warn(f"longitudes span {lon.min():.2f}..{lon.max():.2f} (-180..180 convention); the viewer "
             "expects 0..360 and will roll them")
    dlon = float(abs(lon[1] - lon[0])) if lon.size > 1 else 0.0
    is_global = lon.size > 1 and (lon.max() - lon.min()) >= 360.0 - 1.5 * dlon
    if not is_global:
        log.info("%s: regional grid (lon %.2f..%.2f, lat %.2f..%.2f)", var, lon.min(), lon.max(), lat.min(), lat.max())
    extra = [v for v in ref["data_vars"] if v != var]
    if extra:
        log.debug("%s: files also contain %s", var, extra)
    return {
        "var": var, "files": keep, "problems": problems, "freq": freq, "tags": tags, "missing": missing,
        "format": ref["format"], "dtype": ref_var["dtype"], "dims": list(ref_var["dims"]),
        "attrs": ref_var["attrs"], "global_attrs": ref["global_attrs"],
        "calendar": ref["calendar"], "time_units": ref["time_units"], "coord_names": ref["coord_names"],
        "lat": lat, "lon": lon, "lat_attrs": ref["lat_attrs"], "lon_attrs": ref["lon_attrs"], "global": is_global,
        "level_name": ref_var.get("level_name"), "levels": ref_var.get("levels"),
        "level_attrs": ref_var.get("level_attrs"), "realm": keep[0].get("realm"),
    }


# ---------------------------------------------------------------- build
def _parser_for(fmt: str, forced: str):
    from virtualizarr.parsers import HDFParser, NetCDF3Parser
    if forced == "hdf5" or (forced == "auto" and fmt.startswith("NETCDF4")):
        return HDFParser()
    return NetCDF3Parser()  # NETCDF3_CLASSIC / 64BIT_OFFSET / 64BIT_DATA (needs kerchunk)


def open_virtual(desc: dict, parser_choice: str):
    """Concatenate all files of a variable into one virtual dataset."""
    import xarray as xr
    from virtualizarr import open_virtual_dataset
    from obstore.store import LocalStore
    try:  # VirtualiZarr >= 2.7 moved the registry to obspec_utils
        from obspec_utils.registry import ObjectStoreRegistry
    except ImportError:
        from virtualizarr.registry import ObjectStoreRegistry

    registry = ObjectStoreRegistry({"file://": LocalStore()})
    var = desc["var"]
    parts = []
    prog = Progress(len(desc["files"]), f"{var}: reading chunk references")
    for f in desc["files"]:
        info = f["info"]
        prog.step(f["path"].name)
        # coords/bounds inline, data virtual; other data variables of a
        # multi-variable file are dropped (they get their own group)
        others = [v for v in info["data_vars"] if v != var]
        loadable = [v for v in info["variables"] if v != var and v not in others]
        parser = _parser_for(info["format"], parser_choice)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vds = open_virtual_dataset(f"file://{f['path'].resolve()}", registry=registry, parser=parser,
                                       loadable_variables=loadable, decode_times=True)
        vds = vds.drop_vars([v for v in others if v in vds.variables])
        tn = info["coord_names"]["time"]
        if tn != "time":
            vds = vds.rename({tn: "time"})
        parts.append(vds)
    prog.done()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vds = xr.concat(parts, dim="time", coords="minimal", compat="override",
                        combine_attrs="override", data_vars="minimal")
    return vds


def _containers(descs: list[dict]) -> list[str]:
    """Icechunk resolves virtual chunks through 'containers' keyed by URL
    prefix. One container per top-level directory the (symlink-resolved)
    files really live in, e.g. file:///data/4/... and file:///archive/...,
    so a data tree that symlinks into the archive still resolves."""
    by_top: dict[str, list[str]] = {}
    for d in descs:
        for f in d["files"]:
            real = f["path"].resolve()
            top = "/" + real.parts[1] if len(real.parts) > 1 else "/"
            by_top.setdefault(top, []).append(str(real.parent))
    return sorted(f"file://{os.path.commonpath(v)}/" for v in by_top.values())


def write_store(out: Path, roots: list[Path], descs: list[dict], parser_choice: str, append: bool):
    """Returns the list of virtual chunk container prefixes the store uses."""
    import icechunk as ic

    prefixes = _containers(descs)
    storage = ic.local_filesystem_storage(str(out / "store"))
    cat_path = out / "catalog.json"
    if append and (out / "store").exists() and cat_path.exists():
        old_cat = json.loads(cat_path.read_text())
        old = old_cat.get("virtual_chunk_containers") or [old_cat["virtual_chunk_container"]]
        prefixes = sorted(set(old) | set(prefixes))
        creds = {p: ic.Credentials.LocalFileSystemAccess() for p in prefixes}
        repo = ic.Repository.open(storage, authorize_virtual_chunk_access=creds)
        missing = [p for p in prefixes if p not in old]
        if missing:  # files now live somewhere the store wasn't told about
            config = repo.config
            for p in missing:
                config.set_virtual_chunk_container(ic.VirtualChunkContainer(p, ic.local_filesystem_store(p[len("file://"):])))
            repo = ic.Repository.open(storage, config=config, authorize_virtual_chunk_access=creds)
            repo.save_config()
            log.info("registered new file location(s): %s", ", ".join(missing))
    else:
        creds = {p: ic.Credentials.LocalFileSystemAccess() for p in prefixes}
        config = ic.RepositoryConfig.default()
        for p in prefixes:
            config.set_virtual_chunk_container(ic.VirtualChunkContainer(p, ic.local_filesystem_store(p[len("file://"):])))
        repo = ic.Repository.create(storage, config=config, authorize_virtual_chunk_access=creds)
        append = False
    session = repo.writable_session("main")
    existing = _existing_periods(repo, {d["var"]: d["freq"] for d in descs}) if append else {}
    changed = False
    for d in descs:
        var, freq = d["var"], d["freq"]
        if append and var in existing:
            new = [f for f in d["files"] if all(t not in existing[var] for t in f["tags"])]
            if not new:
                log.info("%s: up to date (%d %s periods)", var, len(existing[var]), freq)
                continue
            last = max(existing[var])
            if all(t > last for f in new for t in f["tags"]):
                # strictly later periods: append along time
                sub = dict(d, files=new)
                vds = open_virtual(sub, parser_choice)
                log.info("%s: appending %d %s period(s) after %s", var, vds.sizes["time"], freq, tag_label(last, freq))
                vds.vz.to_icechunk(session.store, group=var, append_dim="time")
                changed = True
                continue
            # periods before/inside the existing range (e.g. older files added
            # later): rewrite the whole group so time stays monotonic
            log.info("%s: %d new file(s) fall before %s — rebuilding the variable in time order",
                     var, len(new), tag_label(last, freq))
        vds = open_virtual(d, parser_choice)
        log.info("%s: writing %d %s period(s), %d chunk refs, %s", var, vds.sizes["time"], freq,
                 vds.vz.nrefs(), ", ".join(f"{k}={v}" for k, v in vds.sizes.items()))
        vds.vz.to_icechunk(session.store, group=var, mode="w")
        changed = True
    if not changed:
        log.info("store unchanged (nothing to commit)")
        return prefixes
    snap = session.commit(f"nc_catalog {'update' if append else 'build'} {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    log.info("committed snapshot %s", snap)
    return prefixes


def _existing_periods(repo, freqs: dict[str, str]) -> dict[str, set[str]]:
    """{var: set of period tags already in the store}."""
    import xarray as xr
    import zarr
    out = {}
    store = repo.readonly_session("main").store
    root = zarr.open_group(store, mode="r")
    for name in root.group_keys():
        if name not in freqs:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = xr.open_zarr(store, group=name, consolidated=False, use_cftime=True)
        out[name] = {period_tag(t, freqs[name]) for t in ds.time.values}
    return out


def catalog_json(args, roots, descs, prefixes) -> dict:
    variables = {}
    for d in descs:
        freq, tags = d["freq"], d["tags"]
        months = sorted({t[:6] for t in tags}) if freq != "year" else []
        if freq == "month":
            missing_months = [tag_label(m, "month") for m in d["missing"]]
        elif months:  # finer than monthly: months with no data at all
            have = set(months)
            missing_months = [tag_label(m, "month") for m in month_tags(months[0], months[-1]) if m not in have]
        else:
            missing_months = []
        variables[d["var"]] = {
            "long_name": d["attrs"].get("long_name", d["var"]),
            "units": d["attrs"].get("units", ""),
            "realm": d["realm"],
            "dims": d["dims"],
            "dtype": d["dtype"],
            "frequency": freq,
            "start": tag_label(tags[0], freq),
            "end": tag_label(tags[-1], freq),
            "n_times": len(tags),
            "n_months": len(months) if months else len(tags),
            "missing_months": missing_months,
            "n_missing": len(d["missing"]),
            "missing": [tag_label(m, freq) for m in d["missing"][:1000]],
            "n_files": len(d["files"]),
            "format": d["format"],
            "calendar": d["calendar"],
            "levels": None if d["levels"] is None else [float(x) for x in d["levels"]],
            "level_name": d["level_name"],
            "level_units": (d["level_attrs"] or {}).get("units"),
            "attrs": d["attrs"],
            "problems": [f"{lvl}: {msg}" for lvl, msg in d["problems"]],
        }
    first = descs[0]
    g = first["global_attrs"]
    freqs = sorted({d["freq"] for d in descs})
    return {
        "catalog_version": 2,
        "frequency": freqs[0] if len(freqs) == 1 else "mixed",
        "id": args.id,
        "title": args.title or g.get("title") or args.id,
        "description": args.description or "",
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": [str(r.resolve()) for r in roots],
        "pattern": args.pattern,
        "var_from": args.var_from,
        "forced_frequency": getattr(args, "frequency", None),
        "parser": args.parser,
        "virtual_chunk_container": prefixes[0],
        "virtual_chunk_containers": prefixes,
        "grid": {
            "nlat": int(first["lat"].size), "nlon": int(first["lon"].size),
            "lat_min": float(first["lat"].min()), "lat_max": float(first["lat"].max()),
            "lon_min": float(first["lon"].min()), "lon_max": float(first["lon"].max()),
            "dlat": float(abs(first["lat"][1] - first["lat"][0])) if first["lat"].size > 1 else None,
            "dlon": float(abs(first["lon"][1] - first["lon"][0])) if first["lon"].size > 1 else None,
            "lat_units": first["lat_attrs"].get("units"), "lon_units": first["lon_attrs"].get("units"),
            "global": bool(first["global"]),
            "lon_convention": "-180..180" if float(first["lon"].min()) < 0 else "0..360",
        },
        "global_attrs": g,
        "variables": variables,
        "n_problems": sum(len(d["problems"]) for d in descs),
    }


# ---------------------------------------------------------------- CLI
def _setup_logging(logfile: Path | None, verbose: bool):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h)
    h = _ProgressAwareHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)


def run_checks(roots: list[Path], opts) -> list[dict]:
    groups = discover(roots, opts.pattern, opts.var_from)
    if opts.vars:
        groups = {k: v for k, v in groups.items() if k in opts.vars}
        for v in opts.vars:
            if v not in groups:
                log.error("requested variable %r not found", v)
    if not groups:
        raise CatalogError("no files to catalog")
    descs = []
    for var, files in groups.items():
        log.info("checking %s: %d file(s)", var, len(files))
        d = check_variable(var, files, _norm_freq(getattr(opts, "frequency", None)))
        if d["files"]:
            descs.append(d)
        else:
            log.error("%s: dropped (no usable files)", var)
    if not descs:
        raise CatalogError("every variable failed its checks")
    return descs


def execute(cmd: str, roots: list[Path], opts) -> int:
    """check / build / update one catalog. `opts` needs: pattern, var_from,
    vars, parser, out (build/update), id, title, description, keep_going, force."""
    t0 = time.time()
    try:
        descs = run_checks(roots, opts)
    except CatalogError as e:
        log.error("%s", e)
        return 2

    n_err = sum(1 for d in descs for lvl, _ in d["problems"] if lvl == "ERROR")
    n_warn = sum(1 for d in descs for lvl, _ in d["problems"] if lvl == "WARNING")
    log.info("checks done: %d variable(s), %d warning(s), %d error(s)", len(descs), n_warn, n_err)
    for d in descs:
        log.info("  %-12s %s..%s  %s, %d steps / %d files, %s, units %s%s", d["var"],
                 tag_label(d["tags"][0], d["freq"]), tag_label(d["tags"][-1], d["freq"]), d["freq"],
                 len(d["tags"]), len(d["files"]), d["format"], d["attrs"].get("units", "?"),
                 f", {len(d['missing'])} missing" if d["missing"] else "")
    if cmd == "check":
        return 1 if n_err else 0
    if n_err and not opts.keep_going:
        log.error("files were skipped with ERROR; fix them or use --keep-going")
        return 2

    if cmd == "build" and opts.force:
        for child in opts.out.iterdir():
            if child.name != "catalog.log":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    try:
        prefixes = write_store(opts.out, roots, descs, opts.parser, append=(cmd == "update"))
    except Exception as e:  # noqa: BLE001
        log.exception("store write failed: %s", e)
        return 3
    cat = catalog_json(opts, roots, descs, prefixes)
    (opts.out / "catalog.json").write_text(json.dumps(cat, indent=2, default=_py))
    log.info("wrote %s (%.1fs total)", opts.out / "catalog.json", time.time() - t0)
    return 0


def _info(out: Path):
    cat = json.loads((out / "catalog.json").read_text())
    print(f"{cat['id']}: {cat['title']}  (built {cat['built']})")
    print(f"  store: {out / 'store'}")
    print(f"  roots: {', '.join(cat['roots'])}")
    g = cat["grid"]
    print(f"  grid: {g['nlat']} lat x {g['nlon']} lon, lat {g['lat_min']}..{g['lat_max']}, lon {g['lon_min']}..{g['lon_max']}")
    for k, v in cat["variables"].items():
        n_miss = v.get("n_missing", len(v.get("missing_months", [])))
        miss = f", {n_miss} missing" if n_miss else ""
        freq = v.get("frequency", "month")
        print(f"  {k:12s} {v['long_name']} [{v['units']}]  {v['start']}..{v['end']} "
              f"({freq}, {v.get('n_times', v.get('n_months'))} steps, {v['n_files']} files, {v['format']}{miss})")
        for pr in v["problems"]:
            print(f"      {pr}")


CONFIG_EXAMPLE = """{
  "output_root": "/home/jlz/virtualizarr",
  "catalogs": [
    {
      "id": "ecda",
      "title": "ECDA reanalysis",
      "description": "SPEAR c96 ECDA J11 monthly means",
      "roots": ["/data/4/zappalaj/ecda_reanalysis/mo"],
      "pattern": null,
      "var_from": "auto",
      "vars": null,
      "frequency": null,
      "parser": "auto",
      "keep_going": false
    },
    {
      "id": "prism",
      "title": "PRISM daily (CONUS)",
      "description": "PRISM v2 AN81d daily, 1/24 degree",
      "roots": ["/data/4/zappalaj/prism/historical/day/atmos/day/r0i0p0/v2"],
      "pattern": null,
      "var_from": "auto",
      "vars": null,
      "frequency": null,
      "parser": "auto",
      "keep_going": false
    }
  ]
}
"pattern": null lets the tool work out the file names itself (FMS
atmos.YYYYMM-YYYYMM.var.nc, CMIP var_table_..._YYYYMMDD-YYYYMMDD.nc, or
anything else: variables and dates are read from inside the files).
"frequency": null means detect (year, month, day, 12hr, 6hr, 3hr, 1hr)."""


def run_config(config: Path, ids: list[str], check_only: bool, force: bool, verbose: bool) -> int:
    """Build every catalog in the config under output_root/<id>; catalogs
    that already exist are updated (new months appended)."""
    cfg = json.loads(config.read_text())
    root = Path(os.path.expanduser(cfg.get("output_root", "."))).resolve()
    entries = cfg.get("catalogs") or []
    if ids:
        entries = [e for e in entries if e["id"] in ids]
        for i in ids:
            if not any(e["id"] == i for e in entries):
                print(f"no catalog {i!r} in {config}", file=sys.stderr)
                return 2
    if not entries:
        print(f"no catalogs listed in {config}", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for e in entries:
        out = root / e["id"]
        roots = [Path(os.path.expanduser(r)) for r in e["roots"]]
        opts = argparse.Namespace(
            pattern=e.get("pattern") or DEFAULT_PATTERN, var_from=e.get("var_from", "auto"),
            vars=e.get("vars"), frequency=e.get("frequency"), parser=e.get("parser", "auto"), out=out, id=e["id"],
            title=e.get("title"), description=e.get("description"),
            keep_going=bool(e.get("keep_going", False)), force=force,
        )
        exists = (out / "store").exists()
        cmd = "check" if check_only else ("build" if (force or not exists) else "update")
        out.mkdir(parents=True, exist_ok=True)
        _setup_logging(None if check_only else out / "catalog.log", verbose)
        print(f"\n=== {e['id']}: {cmd} -> {out}   ({', '.join(str(r) for r in roots)})", file=sys.stderr)
        rc = execute(cmd, roots, opts)
        results.append((e["id"], cmd, rc, out))
    print("\nsummary:", file=sys.stderr)
    for cid, cmd, rc, out in results:
        print(f"  {cid:12s} {cmd:6s} {'ok' if rc == 0 else f'FAILED (rc {rc})':16s} {out}", file=sys.stderr)
    if not check_only and any(rc == 0 for _, _, rc, _ in results):
        print("\ndatasets.json entries for the viewer:", file=sys.stderr)
        snippet = [{"id": cid, "type": "catalog", "path": str(out),
                    "title": next(e.get("title") for e in entries if e["id"] == cid) or cid}
                   for cid, _, rc, out in results if rc == 0]
        print(json.dumps(snippet, indent=2))
    return max(rc for _, _, rc, _ in results)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, needs_roots=True):
        if needs_roots:
            p.add_argument("roots", nargs="+", type=Path, help="directories to scan (recursively)")
        p.add_argument("--pattern", default=DEFAULT_PATTERN,
                       help="filename regex with (?P<start>)/(?P<end>) date stamps and optional (?P<var>)/(?P<realm>) "
                            "groups; files that do not match are skipped. Default: recognise FMS and CMIP names, "
                            "otherwise read everything from the file contents")
        p.add_argument("--var-from", choices=["auto", "filename", "dir"], default="auto",
                       help="variable name: from the file contents (default; every data variable with a time axis), "
                            "the filename pattern, or the parent directory")
        p.add_argument("--vars", nargs="*", help="only these variables")
        p.add_argument("--frequency", choices=sorted(FREQS), help="force the time frequency instead of detecting it")
        p.add_argument("--parser", choices=["auto", "hdf5", "netcdf3"], default="auto")
        p.add_argument("-v", "--verbose", action="store_true")

    pc = sub.add_parser("check", help="inspect files and report problems; write nothing")
    common(pc)
    pc.add_argument("--log", type=Path)

    pb = sub.add_parser("build", help="check, then write the Icechunk store + catalog.json")
    common(pb)
    pb.add_argument("--out", type=Path, required=True, help="catalog directory (created)")
    pb.add_argument("--id", help="dataset id used by the viewer (default: out dir name)")
    pb.add_argument("--title", help="display title (default: the files' global 'title' attribute)")
    pb.add_argument("--description")
    pb.add_argument("--keep-going", action="store_true", help="build even when some files were skipped with ERROR")
    pb.add_argument("--force", action="store_true", help="overwrite an existing catalog")

    pu = sub.add_parser("update", help="rescan the recorded roots and append new months")
    common(pu, needs_roots=False)
    pu.add_argument("--out", type=Path, required=True)

    pi = sub.add_parser("info", help="print a catalog summary")
    pi.add_argument("out", type=Path)

    pr = sub.add_parser("run", help="build/update every catalog listed in a config file")
    pr.add_argument("--config", type=Path, required=True, help="JSON config (see 'run --example')")
    pr.add_argument("ids", nargs="*", help="only these catalog ids")
    pr.add_argument("--check-only", action="store_true", help="run the checks, write nothing")
    pr.add_argument("--force", action="store_true", help="rebuild existing catalogs from scratch")
    pr.add_argument("-v", "--verbose", action="store_true")

    pe = sub.add_parser("example-config", help="print an example config for 'run'")

    args = ap.parse_args(argv)

    if args.cmd == "example-config":
        print(CONFIG_EXAMPLE)
        return 0
    if args.cmd == "info":
        _info(args.out)
        return 0
    if args.cmd == "run":
        return run_config(args.config, args.ids, args.check_only, args.force, args.verbose)

    if args.cmd == "update":
        cat_path = args.out / "catalog.json"
        if not cat_path.exists():
            ap.error(f"no catalog.json in {args.out}")
        cat = json.loads(cat_path.read_text())
        roots = [Path(r) for r in cat["roots"]]
        args.pattern, args.var_from = cat.get("pattern"), cat.get("var_from") or "auto"
        if args.var_from == "filename" and not args.pattern:
            args.var_from = "auto"
        if cat.get("frequency") and cat["frequency"] != "mixed":
            args.frequency = cat["frequency"]
        args.id, args.title, args.description = cat["id"], cat["title"], cat.get("description")
        args.keep_going, args.force = False, False
        _setup_logging(args.out / "catalog.log", args.verbose)
    elif args.cmd == "build":
        roots = args.roots
        if args.out.exists() and any(args.out.iterdir()) and not args.force:
            ap.error(f"{args.out} exists and is not empty (use --force to rebuild, or 'update')")
        args.out.mkdir(parents=True, exist_ok=True)
        args.id = args.id or args.out.name
        _setup_logging(args.out / "catalog.log", args.verbose)
    else:  # check
        roots = args.roots
        args.keep_going = args.force = False
        _setup_logging(args.log, args.verbose)
    return execute(args.cmd, roots, args)


if __name__ == "__main__":
    sys.exit(main())
