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

Layout expected (FMS/fregrid monthly output, one variable per file):
    <root>/<var>/<realm>.YYYYMM-YYYYMM.<var>.nc        e.g. mo/t_surf/atmos.202005-202005.t_surf.nc
The filename pattern is configurable (--pattern) and the variable can be
taken from the filename (default) or the parent directory.

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

DEFAULT_PATTERN = r"^(?P<realm>[^.]+)\.(?P<start>\d{6})-(?P<end>\d{6})\.(?P<var>.+)\.nc$"

# Names that are coordinates/bounds/bookkeeping, never the data variable.
AUX_NAMES = {"time", "lat", "lon", "latitude", "longitude", "bnds", "nv", "plev", "pfull", "phalf",
             "level", "lev", "time_bnds", "lat_bnds", "lon_bnds", "average_T1", "average_T2",
             "average_DT", "lat_bounds", "lon_bounds", "time_bounds", "height", "member_id"}


class CatalogError(Exception):
    pass


# ---------------------------------------------------------------- discovery
def month_tags(start: str, end: str) -> list[str]:
    """['202005', '202006', ...] inclusive."""
    y, m = int(start[:4]), int(start[4:])
    y1, m1 = int(end[:4]), int(end[4:])
    out = []
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def discover(roots: list[Path], pattern: str, var_from: str) -> dict[str, list[dict]]:
    """Walk the roots; group matching files by variable."""
    rx = re.compile(pattern)
    groups: dict[str, list[dict]] = defaultdict(list)
    n_seen = n_skipped = 0
    for root in roots:
        if not root.is_dir():
            raise CatalogError(f"not a directory: {root}")
        for p in sorted(root.rglob("*.nc")):
            n_seen += 1
            m = rx.match(p.name)
            if not m:
                log.warning("SKIP %s: name does not match pattern %r", p, pattern)
                n_skipped += 1
                continue
            d = m.groupdict()
            var = p.parent.name if var_from == "dir" else d.get("var")
            if not var:
                log.warning("SKIP %s: no variable name in pattern/directory", p)
                n_skipped += 1
                continue
            groups[var].append({"path": p, "start": d["start"], "end": d["end"], "realm": d.get("realm")})
    log.info("discovered %d .nc files under %s: %d usable, %d skipped, %d variables",
             n_seen, ", ".join(str(r) for r in roots), n_seen - n_skipped, n_skipped, len(groups))
    return dict(groups)


# ---------------------------------------------------------------- inspection
def inspect_file(path: Path, var: str) -> dict:
    """Cheap header + coordinate read with netCDF4 (no data variable read)."""
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
                "variables": list(nc.variables), "global_attrs": {k: _py(nc.getncattr(k)) for k in nc.ncattrs()}}
        data_vars = [v for v in nc.variables if v not in AUX_NAMES and nc.variables[v].ndim >= 2]
        info["data_vars"] = data_vars
        if var not in nc.variables:
            raise CatalogError(f"variable {var!r} not in file (has {data_vars})")
        v = nc.variables[var]
        info["dtype"] = str(v.dtype)
        info["var_dims"] = v.dimensions
        info["var_attrs"] = {k: _py(v.getncattr(k)) for k in v.ncattrs()}
        for c in ("lat", "lon"):
            if c not in nc.variables:
                raise CatalogError(f"no {c!r} coordinate (variables: {list(nc.variables)})")
            info[c] = np.asarray(nc.variables[c][:], dtype=float)
            info[f"{c}_attrs"] = {k: _py(nc.variables[c].getncattr(k)) for k in nc.variables[c].ncattrs()}
        if "time" not in nc.variables:
            raise CatalogError("no 'time' coordinate")
        t = nc.variables["time"]
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
        for c in ("plev", "pfull", "level", "lev"):
            if c in nc.variables and c in v.dimensions:
                info["level_name"] = c
                info["levels"] = np.asarray(nc.variables[c][:], dtype=float)
                info["level_attrs"] = {k: _py(nc.variables[c].getncattr(k)) for k in nc.variables[c].ncattrs()}
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


def check_variable(var: str, files: list[dict]) -> dict:
    """Inspect every file of one variable; drop broken ones; return the
    consolidated description plus the list of problems."""
    files.sort(key=lambda f: (f["start"], f["path"].name))
    ref = None
    keep = []
    problems = []  # (level, message)

    def warn(msg):
        problems.append(("WARNING", msg))
        log.warning("%s: %s", var, msg)

    def err(msg):
        problems.append(("ERROR", msg))
        log.error("%s: %s", var, msg)

    seen_months: dict[str, Path] = {}
    prog = Progress(len(files), f"{var}: checking")
    for f in files:
        p = f["path"]
        prog.step(p.name)
        try:
            info = inspect_file(p, var)
        except CatalogError as e:
            err(f"{p.name}: {e} -- file skipped")
            continue
        except Exception as e:  # truncated / corrupt / permission
            err(f"{p.name}: unreadable ({type(e).__name__}: {str(e)[:120]}) -- file skipped")
            continue

        expected = month_tags(f["start"], f["end"])
        nt = len(info["times"])
        if nt != len(expected):
            warn(f"{p.name}: {nt} time steps but the name spans {len(expected)} month(s)")
        # time values must fall inside the months the filename claims;
        # the catalog is indexed by month, so a mismatch makes the file unusable
        outside = [t for t in info["times"] if f"{t.year:04d}{t.month:02d}" not in expected]
        if outside:
            err(f"{p.name}: time value {outside[0]} is outside the name's {f['start']}-{f['end']} -- file skipped")
            continue
        for tag in expected[:nt]:
            if tag in seen_months:
                err(f"{p.name}: month {tag} already provided by {seen_months[tag].name} -- file skipped")
                break
        else:
            for tag in expected[:nt]:
                seen_months[tag] = p
            f["info"] = info
            f["months"] = expected[:nt]
            if ref is None:
                ref = info
            else:
                bad = False
                for c in ("lat", "lon"):
                    if info[c].shape != ref[c].shape or not np.allclose(info[c], ref[c], atol=1e-6):
                        err(f"{p.name}: {c} grid differs from {files[0]['path'].name} "
                            f"({info[c].shape} vs {ref[c].shape}) -- file skipped")
                        bad = True
                if bad:
                    for tag in expected[:nt]:
                        seen_months.pop(tag, None)
                    continue
                if info["format"] != ref["format"]:
                    warn(f"{p.name}: format {info['format']} differs from {ref['format']} "
                         "(mixed formats work but need both parsers)")
                if info["dtype"] != ref["dtype"]:
                    warn(f"{p.name}: dtype {info['dtype']} differs from {ref['dtype']}")
                if info["var_dims"] != ref["var_dims"]:
                    err(f"{p.name}: dimensions {info['var_dims']} differ from {ref['var_dims']} -- file skipped")
                    continue
                for a in ("units", "long_name", "_FillValue", "missing_value"):
                    if info["var_attrs"].get(a) != ref["var_attrs"].get(a):
                        warn(f"{p.name}: {var}:{a} = {info['var_attrs'].get(a)!r} differs from "
                             f"{ref['var_attrs'].get(a)!r} in {files[0]['path'].name}")
                if info["calendar"].lower() != ref["calendar"].lower() or info["time_units"] != ref["time_units"]:
                    warn(f"{p.name}: time encoding ({info['time_units']!r}, {info['calendar']}) differs "
                         f"from ({ref['time_units']!r}, {ref['calendar']})")
                if "levels" in ref and not np.allclose(info.get("levels", []), ref["levels"]):
                    err(f"{p.name}: vertical levels differ -- file skipped")
                    continue
            keep.append(f)
    prog.done(f"{len(keep)} usable, {len(files) - len(keep)} skipped")

    if not keep:
        err("no usable files")
        return {"var": var, "files": [], "problems": problems}

    months = sorted(m for f in keep for m in f["months"])
    full = month_tags(months[0], months[-1])
    missing = sorted(set(full) - set(months))
    if missing:
        warn(f"{len(missing)} missing month(s) in {months[0]}..{months[-1]}: "
             + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""))
    if "units" not in ref["var_attrs"]:
        warn("no units attribute on the data variable")
    if not ref["var_attrs"].get("long_name"):
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
    if len(ref["data_vars"]) > 1:
        log.info("%s: files also contain %s (ignored; catalog one variable per file group)",
                 var, [v for v in ref["data_vars"] if v != var])
    fmt = ref["format"]
    return {
        "var": var, "files": keep, "problems": problems, "months": months, "missing": missing,
        "format": fmt, "dtype": ref["dtype"], "dims": list(ref["var_dims"]),
        "attrs": ref["var_attrs"], "global_attrs": ref["global_attrs"],
        "calendar": ref["calendar"], "time_units": ref["time_units"],
        "lat": lat, "lon": lon, "lat_attrs": ref["lat_attrs"], "lon_attrs": ref["lon_attrs"],
        "level_name": ref.get("level_name"), "levels": ref.get("levels"),
        "level_attrs": ref.get("level_attrs"), "realm": keep[0].get("realm"),
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
        loadable = [v for v in info["variables"] if v != var]  # coords/bounds inline, data virtual
        parser = _parser_for(info["format"], parser_choice)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vds = open_virtual_dataset(f"file://{f['path'].resolve()}", registry=registry, parser=parser,
                                       loadable_variables=loadable, decode_times=True)
        parts.append(vds)
    prog.done()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vds = xr.concat(parts, dim="time", coords="minimal", compat="override",
                        combine_attrs="override", data_vars="minimal")
    return vds


def _container_prefix(roots: list[Path]) -> str:
    common = Path(os.path.commonpath([str(r.resolve()) for r in roots]))
    return f"file://{common}/"


def write_store(out: Path, roots: list[Path], descs: list[dict], parser_choice: str, append: bool):
    import icechunk as ic

    prefix = _container_prefix(roots)
    container_path = prefix[len("file://"):]
    creds = {prefix: ic.Credentials.LocalFileSystemAccess()}
    storage = ic.local_filesystem_storage(str(out / "store"))
    if append and (out / "store").exists():
        repo = ic.Repository.open(storage, authorize_virtual_chunk_access=creds)
    else:
        config = ic.RepositoryConfig.default()
        config.set_virtual_chunk_container(ic.VirtualChunkContainer(prefix, ic.local_filesystem_store(container_path)))
        repo = ic.Repository.create(storage, config=config, authorize_virtual_chunk_access=creds)
        append = False
    session = repo.writable_session("main")
    existing = _existing_months(repo) if append else {}
    changed = False
    for d in descs:
        var = d["var"]
        if append and var in existing:
            new = [f for f in d["files"] if all(m not in existing[var] for m in f["months"])]
            if not new:
                log.info("%s: up to date (%d months)", var, len(existing[var]))
                continue
            sub = dict(d, files=new)
            vds = open_virtual(sub, parser_choice)
            log.info("%s: appending %d month(s)", var, vds.sizes["time"])
            vds.vz.to_icechunk(session.store, group=var, append_dim="time")
            changed = True
        else:
            vds = open_virtual(d, parser_choice)
            log.info("%s: writing %d month(s), %d chunk refs, %s", var, vds.sizes["time"],
                     vds.vz.nrefs(), ", ".join(f"{k}={v}" for k, v in vds.sizes.items()))
            vds.vz.to_icechunk(session.store, group=var, mode="w")
            changed = True
    if not changed:
        log.info("store unchanged (nothing to commit)")
        return prefix
    snap = session.commit(f"nc_catalog {'update' if append else 'build'} {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    log.info("committed snapshot %s", snap)
    return prefix


def _existing_months(repo) -> dict[str, set[str]]:
    import xarray as xr
    import zarr
    out = {}
    store = repo.readonly_session("main").store
    root = zarr.open_group(store, mode="r")
    for name in root.group_keys():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = xr.open_zarr(store, group=name, consolidated=False)
        out[name] = {f"{t.year:04d}-{t.month:02d}".replace("-", "") for t in ds.time.values}
    return out


def catalog_json(args, roots, descs, prefix) -> dict:
    variables = {}
    for d in descs:
        variables[d["var"]] = {
            "long_name": d["attrs"].get("long_name", d["var"]),
            "units": d["attrs"].get("units", ""),
            "realm": d["realm"],
            "dims": d["dims"],
            "dtype": d["dtype"],
            "start": f"{d['months'][0][:4]}-{d['months'][0][4:]}",
            "end": f"{d['months'][-1][:4]}-{d['months'][-1][4:]}",
            "n_months": len(d["months"]),
            "missing_months": [f"{m[:4]}-{m[4:]}" for m in d["missing"]],
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
    return {
        "catalog_version": 1,
        "id": args.id,
        "title": args.title or g.get("title") or args.id,
        "description": args.description or "",
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": [str(r.resolve()) for r in roots],
        "pattern": args.pattern,
        "var_from": args.var_from,
        "parser": args.parser,
        "virtual_chunk_container": prefix,
        "grid": {
            "nlat": int(first["lat"].size), "nlon": int(first["lon"].size),
            "lat_min": float(first["lat"].min()), "lat_max": float(first["lat"].max()),
            "lon_min": float(first["lon"].min()), "lon_max": float(first["lon"].max()),
            "dlat": float(abs(first["lat"][1] - first["lat"][0])) if first["lat"].size > 1 else None,
            "dlon": float(abs(first["lon"][1] - first["lon"][0])) if first["lon"].size > 1 else None,
            "lat_units": first["lat_attrs"].get("units"), "lon_units": first["lon_attrs"].get("units"),
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
        d = check_variable(var, files)
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
        log.info("  %-12s %s..%s  %d months / %d files, %s, units %s%s", d["var"], d["months"][0], d["months"][-1],
                 len(d["months"]), len(d["files"]), d["format"], d["attrs"].get("units", "?"),
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
        prefix = write_store(opts.out, roots, descs, opts.parser, append=(cmd == "update"))
    except Exception as e:  # noqa: BLE001
        log.exception("store write failed: %s", e)
        return 3
    cat = catalog_json(opts, roots, descs, prefix)
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
        miss = f", {len(v['missing_months'])} missing" if v["missing_months"] else ""
        print(f"  {k:12s} {v['long_name']} [{v['units']}]  {v['start']}..{v['end']} "
              f"({v['n_months']} months, {v['n_files']} files, {v['format']}{miss})")
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
      "var_from": "filename",
      "vars": null,
      "parser": "auto",
      "keep_going": false
    }
  ]
}"""


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
            pattern=e.get("pattern") or DEFAULT_PATTERN, var_from=e.get("var_from", "filename"),
            vars=e.get("vars"), parser=e.get("parser", "auto"), out=out, id=e["id"],
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
        p.add_argument("--pattern", default=DEFAULT_PATTERN, help="filename regex with start/end (YYYYMM) and var groups")
        p.add_argument("--var-from", choices=["filename", "dir"], default="filename",
                       help="take the variable name from the filename (default) or the parent directory")
        p.add_argument("--vars", nargs="*", help="only these variables")
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
        args.pattern, args.var_from = cat["pattern"], cat["var_from"]
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
