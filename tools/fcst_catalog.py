#!/usr/bin/env python
"""Catalog a tree of SPEAR-style *forecast* output for the viewer.

A forecast tree has one directory per initialisation, one per ensemble
member under it, and one NetCDF file per variable holding the whole
forecast (a year of 6-hourly means in the SPEAR case):

    <root>/.../i<YYYYMMDD>_<label>[_<variant>]/pp_ens_<NN>/<pp-path>/<comp>.<start>-<end>.<var>.nc

The catalog is an Icechunk store of *virtual* chunk references into those
files (nothing is copied) with one group per variable and initialisation,
``<var>/i<YYYYMMDD>``, dimensions ``(member, time, lat, lon)`` in valid
time, plus ``catalog.json`` describing inits, members, variables and the
files behind every group.

    python tools/fcst_catalog.py build /data/4/.../spear_med --out /data/4/<you>/catalogs/spear_fcst \\
        --id spear_fcst --title "SPEAR forecasts"
    python tools/fcst_catalog.py build ...   # again later: only groups whose files changed are rewritten
    python tools/fcst_catalog.py check /data/4/.../spear_med          # report, write nothing
    python tools/fcst_catalog.py info /data/4/<you>/catalogs/spear_fcst

Rerunnable by design: the tree is never walked in full (the SPEAR tree has
850k mostly empty directories). The init directories are listed, then the
one known leaf directory per member is probed, which takes seconds. A
member or variable that appears in an existing init rewrites just that
init's group; a new init adds a group; everything else is left alone.

Quirks handled: ``<comp>.<start>-<end>.time.nc`` files (time axis only) are
skipped, as are directory names with shell leftovers such as ``pp_ens_0$``.
An init present in several variants (base, ``_update``, ``_rerun``) is taken
from the variant listed first in ``--variant-priority``.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_catalog import (CatalogError, Progress, _parser_for, _py, _setup_logging, detect_frequency,  # noqa: E402
                        inspect_file, log as _nclog)

log = logging.getLogger("nc_catalog")  # share the handler setup with nc_catalog
assert log is _nclog

INIT_RX = re.compile(r"^i(?P<date>\d{8})_(?P<label>.+?)(?:_(?P<variant>update|rerun|[a-z]+))?$")
MEMBER_RX = re.compile(r"^pp_ens_(?P<n>\d+)$")
FILE_RX = re.compile(r"^(?P<comp>[^.]+)\.(?P<start>\d{8,12})-(?P<end>\d{8,12})\.(?P<var>[^.]+)\.nc$")
DEFAULT_PP = "atmos_4xdaily_avg/ts/6hr/1yr"
DEFAULT_PRIORITY = ["update", "rerun", "base"]
SKIP_VARS = {"time"}


# ---------------------------------------------------------------- discovery
def find_init_dirs(root: Path, max_depth: int = 4) -> list[Path]:
    """Directories named i<YYYYMMDD>_... at most `max_depth` levels below
    root. Only listdir calls; never descends into an init directory."""
    found = []
    frontier = [(root, 0)]
    while frontier:
        d, depth = frontier.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            log.warning("cannot list %s: %s", d, e)
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=True):
                continue
            if INIT_RX.match(e.name):
                found.append(Path(e.path))
            elif depth < max_depth and not e.name.startswith("."):
                frontier.append((Path(e.path), depth + 1))
    return sorted(found)


def discover(root: Path, pp_path: str, priority: list[str], vars_only: list[str] | None) -> dict[str, dict]:
    """{init_id: {"init", "variant", "dir", "members": {NN: {var: file_rec}}}}.
    file_rec = {"path", "size", "mtime", "start", "end", "comp"}."""
    root = root.resolve()  # recorded paths must not depend on the working directory
    init_dirs = find_init_dirs(root)
    if not init_dirs:
        raise CatalogError(f"no i<YYYYMMDD>_... directories under {root}")
    log.info("found %d init directories under %s", len(init_dirs), root)
    by_date: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for d in init_dirs:
        m = INIT_RX.match(d.name)
        by_date[m["date"]].append((m["variant"] or "base", d))
    inits: dict[str, dict] = {}
    rank = {v: i for i, v in enumerate(priority)}
    prog = Progress(len(by_date), "probing init directories")
    n_files = 0
    for date, cands in sorted(by_date.items()):
        prog.step(date)
        cands.sort(key=lambda c: rank.get(c[0], len(rank)))
        if len(cands) > 1:
            log.warning("init %s exists as %s; using %s", date, ", ".join(v for v, _ in cands), cands[0][0])
        variant, idir = cands[0]
        members: dict[str, dict] = {}
        try:
            entries = sorted(os.scandir(idir), key=lambda e: e.name)
        except OSError as e:
            log.warning("cannot list %s: %s", idir, e)
            continue
        for e in entries:
            mm = MEMBER_RX.match(e.name)
            if not mm or not e.is_dir():
                continue
            leaf = Path(e.path) / pp_path
            if not leaf.is_dir():
                continue
            mem = f"{int(mm['n']):02d}"
            files: dict[str, dict] = {}
            for f in sorted(os.scandir(leaf), key=lambda x: x.name):
                fm = FILE_RX.match(f.name)
                if not fm or not f.is_file():
                    continue
                var = fm["var"]
                if var in SKIP_VARS or (vars_only and var not in vars_only):
                    continue
                st = f.stat()
                if var in files:
                    log.warning("%s/%s: several files for %s; keeping %s", idir.name, e.name, var, files[var]["path"].name)
                    continue
                files[var] = {"path": Path(f.path), "size": st.st_size, "mtime": int(st.st_mtime),
                              "start": fm["start"], "end": fm["end"], "comp": fm["comp"]}
                n_files += 1
            if files:
                members[mem] = files
        if members:
            iid = f"i{date}"
            inits[iid] = {"init": f"{date[:4]}-{date[4:6]}-{date[6:]}", "variant": variant, "dir": idir,
                          "members": members}
    prog.done(f"{len(inits)} inits with data, {n_files} files")
    return inits


# ---------------------------------------------------------------- checks
def _file_key(rec: dict) -> list:
    return [str(rec["path"]), rec["size"], rec["mtime"]]


def group_signature(inits: dict, iid: str, var: str) -> dict[str, list]:
    """{member: [path, size, mtime]} for one (var, init) group."""
    return {m: _file_key(files[var]) for m, files in sorted(inits[iid]["members"].items()) if var in files}


def check_group(var: str, iid: str, info: dict, members: dict[str, dict], ref: dict | None) -> tuple[list, list]:
    """Inspect every member file of a (var, init) group. Returns (kept
    [(member, rec, info)], problems [(level, msg)]). `ref` is the grid /
    attrs reference from the first good file of this variable."""
    kept, problems = [], []
    first = None
    for mem, rec in sorted(members.items()):
        p = rec["path"]
        try:
            fi = inspect_file(p)
        except CatalogError as e:
            problems.append(("ERROR", f"{iid} member {mem}: {p.name}: {e} -- skipped"))
            continue
        except Exception as e:  # noqa: BLE001
            problems.append(("ERROR", f"{iid} member {mem}: {p.name}: unreadable ({type(e).__name__}: {str(e)[:100]}) -- skipped"))
            continue
        if var not in fi["vars"]:
            problems.append(("ERROR", f"{iid} member {mem}: {p.name}: no variable {var!r} (has {fi['data_vars']}) -- skipped"))
            continue
        if not fi["times"]:
            problems.append(("ERROR", f"{iid} member {mem}: {p.name}: empty time axis -- skipped"))
            continue
        cmp = ref or first
        if cmp is not None:
            bad = False
            for c in ("lat", "lon"):
                if fi[c].shape != cmp[c].shape or not np.allclose(fi[c], cmp[c], atol=1e-6):
                    problems.append(("ERROR", f"{iid} member {mem}: {p.name}: {c} grid {fi[c].shape} differs from {cmp[c].shape} -- skipped"))
                    bad = True
            if tuple(fi["vars"][var]["dims"]) != tuple(cmp["dims"]):
                problems.append(("ERROR", f"{iid} member {mem}: {p.name}: dims {fi['vars'][var]['dims']} differ from {cmp['dims']} -- skipped"))
                bad = True
            if bad:
                continue
            if first is not None and len(fi["times"]) != len(first["times"]):
                problems.append(("ERROR", f"{iid} member {mem}: {p.name}: {len(fi['times'])} time steps, member "
                                          f"{kept[0][0]} has {len(first['times'])} -- skipped"))
                continue
            if first is not None and fi["time_units"] != first["time_units"]:
                problems.append(("WARNING", f"{iid} member {mem}: {p.name}: time units {fi['time_units']!r} differ from {first['time_units']!r}"))
            for a in ("units", "_FillValue", "missing_value"):
                if fi["vars"][var]["attrs"].get(a) != cmp["attrs"].get(a):
                    problems.append(("WARNING", f"{iid} member {mem}: {p.name}: {var}:{a} = {fi['vars'][var]['attrs'].get(a)!r} differs from {cmp['attrs'].get(a)!r}"))
        if first is None:
            first = {"lat": fi["lat"], "lon": fi["lon"], "dims": fi["vars"][var]["dims"],
                     "attrs": fi["vars"][var]["attrs"], "times": fi["times"], "time_units": fi["time_units"],
                     "info": fi}
        kept.append((mem, rec, fi))
    return kept, problems


# ---------------------------------------------------------------- store
def open_group_virtual(var: str, kept: list, parser_choice: str):
    """Concatenate the member files of one (var, init) along a new
    'member' dimension; coords inline, data virtual."""
    import xarray as xr
    from virtualizarr import open_virtual_dataset
    from obstore.store import LocalStore
    try:
        from obspec_utils.registry import ObjectStoreRegistry
    except ImportError:
        from virtualizarr.registry import ObjectStoreRegistry

    registry = ObjectStoreRegistry({"file://": LocalStore()})
    parts, names = [], []
    for mem, rec, info in kept:
        others = [v for v in info["data_vars"] if v != var]
        loadable = [v for v in info["variables"] if v != var and v not in others]
        parser = _parser_for(info["format"], parser_choice)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vds = open_virtual_dataset(f"file://{rec['path'].resolve()}", registry=registry, parser=parser,
                                       loadable_variables=loadable, decode_times=True)
        drop = [v for v in vds.variables if v != var and v not in ("time", "lat", "lon")
                and v not in info["coord_names"].values()]
        vds = vds.drop_vars([v for v in others if v in vds.variables] + drop)
        ren = {info["coord_names"][k]: k for k in ("time", "lat", "lon") if info["coord_names"][k] != k}
        if ren:
            vds = vds.rename(ren)
        parts.append(vds)
        names.append(mem)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vds = xr.concat(parts, dim="member", data_vars=[var], coords="minimal", compat="override",
                        combine_attrs="override")
    vds = vds.assign_coords(member=("member", np.array(names)))
    vds[var].attrs.setdefault("members", ",".join(names))
    return vds


def _containers(paths: list[Path]) -> list[str]:
    by_top: dict[str, list[str]] = {}
    for p in paths:
        real = p.resolve()
        top = "/" + real.parts[1] if len(real.parts) > 1 else "/"
        by_top.setdefault(top, []).append(str(real.parent))
    return sorted(f"file://{os.path.commonpath(v)}/" for v in by_top.values())


def open_repo(out: Path, prefixes: list[str], create: bool):
    import icechunk as ic
    storage = ic.local_filesystem_storage(str(out / "store"))
    creds = {p: ic.Credentials.LocalFileSystemAccess() for p in prefixes}
    if create:
        config = ic.RepositoryConfig.default()
        for p in prefixes:
            config.set_virtual_chunk_container(ic.VirtualChunkContainer(p, ic.local_filesystem_store(p[len("file://"):])))
        return ic.Repository.create(storage, config=config, authorize_virtual_chunk_access=creds)
    repo = ic.Repository.open(storage, authorize_virtual_chunk_access=creds)
    known = {c.url_prefix for c in (repo.config.virtual_chunk_containers or {}).values()} \
        if isinstance(repo.config.virtual_chunk_containers, dict) else \
        {c.url_prefix for c in (repo.config.virtual_chunk_containers or [])}
    missing = [p for p in prefixes if p not in known]
    if missing:
        config = repo.config
        for p in missing:
            config.set_virtual_chunk_container(ic.VirtualChunkContainer(p, ic.local_filesystem_store(p[len("file://"):])))
        repo = ic.Repository.open(storage, config=config, authorize_virtual_chunk_access=creds)
        repo.save_config()
        log.info("registered new file location(s): %s", ", ".join(missing))
    return repo


# ---------------------------------------------------------------- main flow
def _load_old(out: Path) -> dict | None:
    p = out / "catalog.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning("existing catalog.json unreadable (%s); rebuilding", e)
    return None


def execute(cmd: str, root: Path, opts) -> int:
    t0 = time.time()
    try:
        inits = discover(root, opts.pp_path, opts.variant_priority, opts.vars)
    except CatalogError as e:
        log.error("%s", e)
        return 2
    variables = sorted({v for i in inits.values() for files in i["members"].values() for v in files})
    log.info("%d init(s), %d variable(s): %s", len(inits), len(variables), ", ".join(variables))
    for iid, i in inits.items():
        log.debug("  %s %-6s members %s", iid, i["variant"], ",".join(sorted(i["members"])))

    old = None if (cmd == "check" or opts.force) else _load_old(opts.out)
    old_groups = (old or {}).get("groups", {})
    old_inits = (old or {}).get("inits", {})
    create = old is None

    # which (var, init) groups need (re)writing
    todo, unchanged = [], 0
    for var in variables:
        for iid in inits:
            sig = group_signature(inits, iid, var)
            if not sig:
                continue
            key = f"{var}/{iid}"
            if not create and old_groups.get(key, {}).get("files") == sig:
                unchanged += 1
                continue
            todo.append((var, iid, sig))
    if not create:
        gone = [k for k in old_groups if k.split("/")[0] in variables and k.split("/")[1] not in inits]
        for k in gone:
            log.warning("%s: files no longer found; group kept in the store but dropped from catalog.json", k)
    log.info("%d group(s) to write, %d unchanged", len(todo), unchanged)

    # checks
    descs = {}        # key -> {"kept", "problems"}
    ref: dict[str, dict] = {}
    n_err = n_warn = 0
    prog = Progress(len(todo), "checking member files")
    for var, iid, sig in todo:
        prog.step(f"{var}/{iid}")
        kept, problems = check_group(var, iid, inits[iid], {m: f[var] for m, f in inits[iid]["members"].items() if var in f},
                                     ref.get(var))
        for lvl, msg in problems:
            (log.error if lvl == "ERROR" else log.warning)("%s: %s", var, msg)
        n_err += sum(1 for lvl, _ in problems if lvl == "ERROR")
        n_warn += sum(1 for lvl, _ in problems if lvl == "WARNING")
        if kept and var not in ref:
            fi = kept[0][2]
            ref[var] = {"lat": fi["lat"], "lon": fi["lon"], "dims": fi["vars"][var]["dims"],
                        "attrs": fi["vars"][var]["attrs"], "info": fi}
        if kept:
            descs[f"{var}/{iid}"] = {"var": var, "iid": iid, "kept": kept, "problems": problems,
                                     "files": {m: _file_key(rec) for m, rec, _ in kept}}
        else:
            log.error("%s/%s: no usable member file", var, iid)
    prog.done(f"{len(descs)} group(s) ok, {n_warn} warning(s), {n_err} error(s)")
    if cmd == "check":
        return 1 if n_err else 0
    if n_err and not opts.keep_going:
        log.error("files were skipped with ERROR; fix them or use --keep-going")
        return 2
    if not descs and not create:
        log.info("catalog up to date (%.1fs)", time.time() - t0)
        _write_catalog(opts, root, inits, variables, old_groups, old, ref, [], t0)
        return 0
    if not descs:
        log.error("nothing to write")
        return 2

    # store
    if opts.force and opts.out.exists():
        for child in opts.out.iterdir():
            if child.name != "catalog.log":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    opts.out.mkdir(parents=True, exist_ok=True)
    all_paths = [rec["path"] for d in descs.values() for _, rec, _ in d["kept"]]
    prefixes = _containers(all_paths)
    if old:
        prefixes = sorted(set(prefixes) | set(old.get("virtual_chunk_containers") or []))
    try:
        repo = open_repo(opts.out, prefixes, create=create or not (opts.out / "store").exists())
        session = repo.writable_session("main")
        prog = Progress(len(descs), "writing chunk references")
        groups = dict(old_groups)
        for key, d in descs.items():
            prog.step(key)
            vds = open_group_virtual(d["var"], d["kept"], opts.parser)
            vds.vz.to_icechunk(session.store, group=key, mode="w")
            t = vds.time.values
            groups[key] = {"var": d["var"], "init": d["iid"], "members": [m for m, _, _ in d["kept"]],
                           "n_times": int(vds.sizes["time"]), "start": _stamp(t[0]), "end": _stamp(t[-1]),
                           "files": d["files"], "n_refs": int(vds.vz.nrefs()),
                           "problems": [f"{lvl}: {msg}" for lvl, msg in d["problems"]]}
            log.debug("%s: members %s, %d steps, %d refs", key, groups[key]["members"], groups[key]["n_times"], groups[key]["n_refs"])
        prog.done()
        snap = session.commit(f"fcst_catalog {'build' if create else 'update'} {datetime.now(timezone.utc).isoformat(timespec='seconds')}: {len(descs)} group(s)")
        log.info("committed snapshot %s", snap)
    except Exception as e:  # noqa: BLE001
        log.exception("store write failed: %s", e)
        return 3
    _write_catalog(opts, root, inits, variables, groups, old, ref, prefixes, t0)
    return 0


def _stamp(t) -> str:
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}"


def _write_catalog(opts, root, inits, variables, groups, old, ref, prefixes, t0):
    # per-init summary from the groups actually in the store
    inits_out = {}
    for iid, i in inits.items():
        gs = {k: g for k, g in groups.items() if g["init"] == iid}
        if not gs:
            continue
        members = sorted({m for g in gs.values() for m in g["members"]})
        inits_out[iid] = {"init": i["init"], "variant": i["variant"], "dir": str(i["dir"]),
                          "members": members, "n_members": len(members),
                          "variables": sorted(g["var"] for g in gs.values()),
                          "start": min(g["start"] for g in gs.values()), "end": max(g["end"] for g in gs.values()),
                          "n_times": max(g["n_times"] for g in gs.values())}
    vars_out = {}
    old_vars = (old or {}).get("variables", {})
    for var in variables:
        gs = [g for g in groups.values() if g["var"] == var]
        if not gs:
            continue
        r = ref.get(var)
        if r is None and var in old_vars:
            entry = dict(old_vars[var])
        elif r is not None:
            fi = r["info"]
            freq, how = detect_frequency(fi["times"], fi.get("bounds_days"), None, None)
            entry = {"long_name": r["attrs"].get("long_name", var), "units": r["attrs"].get("units", ""),
                     "realm": "atmos", "dims": ["member"] + list(r["dims"]), "dtype": fi["vars"][var]["dtype"],
                     "frequency": freq or "6hr", "format": fi["format"], "calendar": fi["calendar"],
                     "levels": None, "level_name": None, "level_units": None, "attrs": r["attrs"]}
        else:
            continue
        entry.update({"n_inits": len(gs), "n_files": sum(len(g["members"]) for g in gs),
                      "start": min(g["start"] for g in gs), "end": max(g["end"] for g in gs),
                      "inits": sorted(g["init"] for g in gs),
                      "problems": [p for g in gs for p in g.get("problems", [])][:200]})
        vars_out[var] = entry
    any_ref = next(iter(ref.values()), None)
    if any_ref is not None:
        fi = any_ref["info"]
        lat, lon = any_ref["lat"], any_ref["lon"]
        grid = {"nlat": int(lat.size), "nlon": int(lon.size),
                "lat_min": float(lat.min()), "lat_max": float(lat.max()),
                "lon_min": float(lon.min()), "lon_max": float(lon.max()),
                "dlat": float(abs(lat[1] - lat[0])) if lat.size > 1 else None,
                "dlon": float(abs(lon[1] - lon[0])) if lon.size > 1 else None,
                "lat_units": fi["lat_attrs"].get("units"), "lon_units": fi["lon_attrs"].get("units"),
                "global": bool(lon.size > 1 and (lon.max() - lon.min()) >= 360.0 - 1.5 * abs(lon[1] - lon[0])),
                "lon_convention": "-180..180" if float(lon.min()) < 0 else "0..360"}
        global_attrs = fi["global_attrs"]
        calendar = fi["calendar"]
    else:
        grid, global_attrs, calendar = old["grid"], old["global_attrs"], old["calendar"]
    lead = max((i["n_times"] for i in inits_out.values()), default=0)
    cat = {
        "catalog_version": 3,
        "kind": "forecast",
        "id": opts.id or opts.out.name,
        "title": opts.title or (old or {}).get("title") or opts.id or opts.out.name,
        "description": opts.description or (old or {}).get("description", ""),
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": [str(root.resolve())],
        "pp_path": opts.pp_path,
        "variant_priority": opts.variant_priority,
        "parser": opts.parser,
        "virtual_chunk_containers": prefixes or (old or {}).get("virtual_chunk_containers", []),
        "virtual_chunk_container": (prefixes or (old or {}).get("virtual_chunk_containers") or [None])[0],
        "frequency": next(iter(vars_out.values()))["frequency"] if vars_out else "6hr",
        "calendar": calendar,
        "max_steps": lead,
        "n_inits": len(inits_out), "n_groups": len(groups),
        "members": sorted({m for i in inits_out.values() for m in i["members"]}),
        "grid": grid,
        "global_attrs": global_attrs,
        "variables": vars_out,
        "inits": inits_out,
        "groups": groups,
    }
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "catalog.json").write_text(json.dumps(cat, indent=1, default=_py))
    log.info("wrote %s: %d inits, %d variables, %d groups (%.1fs total)", opts.out / "catalog.json",
             len(inits_out), len(vars_out), len(groups), time.time() - t0)


def info(out: Path) -> int:
    cat = _load_old(out)
    if not cat:
        print(f"no catalog at {out}")
        return 2
    print(f"{cat['title']} ({cat['id']}), kind {cat.get('kind')}, built {cat['built']}")
    print(f"  roots: {', '.join(cat['roots'])}")
    g = cat["grid"]
    print(f"  grid: {g['nlat']} x {g['nlon']}, {cat['frequency']}, calendar {cat['calendar']}, up to {cat['max_steps']} steps per forecast")
    print(f"  {cat['n_inits']} inits, members {', '.join(cat['members'])}")
    by_variant = defaultdict(list)
    for iid, i in cat["inits"].items():
        by_variant[i["variant"]].append(i["init"])
    for v, ds in sorted(by_variant.items()):
        print(f"    {v:7s} {len(ds):4d} inits  {ds[0]} .. {ds[-1]}")
    for var, v in cat["variables"].items():
        print(f"  {var:10s} {v['n_inits']:4d} inits / {v['n_files']:4d} files  {v['units']:12s} {v['long_name']}")
    probs = [p for v in cat["variables"].values() for p in v.get("problems", [])]
    if probs:
        print(f"  {len(probs)} recorded problem(s), e.g. {probs[0]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("root", type=Path, help="forecast tree (any directory above the i<YYYYMMDD>_... dirs)")
        p.add_argument("--pp-path", default=DEFAULT_PP, help=f"leaf directory under each member (default {DEFAULT_PP})")
        p.add_argument("--vars", nargs="*", help="only these variables")
        p.add_argument("--variant-priority", nargs="+", default=DEFAULT_PRIORITY,
                       help="when an init date exists in several variants, which wins (default: update rerun base)")
        p.add_argument("--parser", choices=["auto", "hdf5", "netcdf3"], default="auto")
        p.add_argument("-v", "--verbose", action="store_true")

    pc = sub.add_parser("check", help="probe the tree and inspect the files; write nothing")
    common(pc)
    pc.add_argument("--log", type=Path)
    pb = sub.add_parser("build", help="build the catalog, or update it if it exists (only changed groups are rewritten)")
    common(pb)
    pb.add_argument("--out", type=Path, required=True)
    pb.add_argument("--id")
    pb.add_argument("--title")
    pb.add_argument("--description")
    pb.add_argument("--keep-going", action="store_true")
    pb.add_argument("--force", action="store_true", help="discard the existing catalog and rebuild everything")
    pi = sub.add_parser("info", help="print a catalog summary")
    pi.add_argument("out", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "info":
        return info(a.out)
    logfile = a.log if a.cmd == "check" else a.out / "catalog.log"
    _setup_logging(logfile, a.verbose)
    for attr, default in (("out", None), ("id", None), ("title", None), ("description", None),
                          ("keep_going", False), ("force", False)):
        if not hasattr(a, attr):
            setattr(a, attr, default)
    return execute(a.cmd, a.root, a)


if __name__ == "__main__":
    sys.exit(main())
