# Climate Data Viewer (windy-viewer)
## Status: UNDER DEVELOPMENT/PROTOTYPE

An interactive Windy-style viewer for gridded monthly climate data. It grew
out of the SPEAR-MED large-ensemble viewer (`spear-windy`) and keeps every
feature of it, but nothing about a particular dataset is built in any more:
titles, variables, experiments, ensemble members, pressure levels, wind
components and unit conversions are all discovered from the data. One
running server can expose several datasets, switched from the panel.

Two kinds of data source are supported, behind one interface (`windy_viewer/sources.py`):

| kind | what it reads | typical use |
|---|---|---|
| `arraylake` | an ArrayLake/Icechunk repo (Zarr groups `<experiment>/<group>`) | the public SPEAR large ensemble |
| `catalog` | a local Icechunk store built by `tools/nc_catalog.py` from a directory of NetCDF files | model output / reanalysis on an HPC filesystem |

The catalog kind uses **VirtualiZarr**: the store holds *references* into
the original `.nc` files (nothing is copied), and the viewer reads each
month's chunk straight from its file through the same zarr/icechunk code
path it uses for ArrayLake.

**Features**: monthly variables (surface and pressure-level, any realm) ·
ensemble member / mean / spread / deviation statistics when the dataset has
an ensemble · A−B compare mode · wind particle animation when the dataset
has (u, v) components · rotatable 3-D globe · virtual stations with
time-series extraction and merged plots · box extraction over any month range ·
year playback · value filtering · unit conversion · NetCDF/CSV downloads
carrying the source's metadata · **SPEAK**, a rate-limited RAG chatbot that
sees the on-screen statistics and the dataset description.

## Layout

```
windy_viewer/server.py     FastAPI server (port 8601) — the JSON API + static frontend
windy_viewer/sources.py    data sources: ArrayLakeSource, CatalogSource, unit rules, datasets.json loader
windy_viewer/static/       Leaflet frontend (app.js builds every control from /api/meta)
tools/nc_catalog.py        build/check/update a virtual Icechunk catalog from NetCDF files
tools/make_mock_ecda.py    generate a fake copy of an HPC directory (same names/headers) for local testing
datasets.example.json      example dataset configuration (copy to datasets.json)
rag-service/               document-retrieval API for SPEAK (port 8002, internal)
requirements.txt           one environment for both services
Containerfile              podman/docker build
```

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate   # or conda
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
cp .env.example .env                            # tokens / keys
cp datasets.example.json datasets.json          # which datasets to serve (optional, see below)
python windy_viewer/server.py                   # http://localhost:8601
```

Without a `datasets.json` the viewer serves the SPEAR large ensemble from
ArrayLake exactly as `spear-windy` does (needs `ARRAYLAKE_TOKEN`).

## Configuring datasets

`datasets.json` (or the file named by `VIEWER_DATASETS`) lists the sources
in the order the panel shows them; the first is the default:

```json
{
  "datasets": [
    { "id": "spear", "type": "arraylake",
      "repo": "GFDL/noaa-gfdl-spear-large-ensembles-pds", "branch": "main" },
    { "id": "ecda", "type": "catalog", "path": "/home/me/catalogs/ecda",
      "title": "ECDA reanalysis", "subtitle": "SPEAR c96 ECDA J11 · monthly",
      "variables": { "t_surf": { "label": "Surface temperature" } } }
  ]
}
```

* A bare SPEAR entry (that repo, no `variables`) inherits the curated
  variable labels of the original viewer. Any other ArrayLake repo is
  discovered from its store: give it `experiments` (`{name: {start, end}}`)
  and `groups` (Zarr groups to scan); every `(time, lat, lon)` variable is
  offered.
* `variables` entries are optional overrides per variable: `label`, `long`,
  `units`/`scale`/`offset` (display conversion), `family`.
* `wind: {"sfc": ["u_ref", "v_ref"], "plev": ["ucomp", "vcomp"]}` names the
  components for the particle animation when the auto-detected pairs
  (`uas/vas`, `u_ref/v_ref`, `ua/va`, `ucomp/vcomp`, ...) don't apply.
* `link` / `home` become the "Access Data" button and the title link;
  `description` and `chat_notes` feed SPEAK's system prompt.
* A dataset that fails to open (no network, missing catalog) is reported in
  `/api/meta` and skipped in the panel; the others still work.

Display units are derived from each variable's `units` attribute
(`K`/`deg_k` → °C, `kg m-2 s-1`/`kg/m2/s` → mm/day, `Pa` → hPa, ...);
unknown units are shown as-is with an auto-scaled colour ramp.

## Local NetCDF data (HPC): building a catalog

Given monthly files laid out as FMS/fregrid writes them, one variable per file:

```
/data/.../ecda_reanalysis/mo/t_surf/atmos.202005-202005.t_surf.nc
/data/.../ecda_reanalysis/mo/t_surf/atmos.202006-202006.t_surf.nc
/data/.../ecda_reanalysis/mo/precip/atmos.202005-202005.precip.nc
...
```

```bash
# 1. inspect only — every file is opened and checked, nothing written
python tools/nc_catalog.py check /data/.../ecda_reanalysis/mo

# 2. build the catalog (Icechunk store + catalog.json + catalog.log)
python tools/nc_catalog.py build /data/.../ecda_reanalysis/mo \
    --out ~/catalogs/ecda --id ecda --title "ECDA reanalysis"

# 3. later, when new months have landed: append them
python tools/nc_catalog.py update --out ~/catalogs/ecda

python tools/nc_catalog.py info ~/catalogs/ecda      # summary
```

Then point `datasets.json` at `~/catalogs/ecda` and start the server.

**Config-driven (recommended on the HPC):** list the source directories and
one output root in a JSON file and let `run` keep every catalog in sync —
new catalogs are built, existing ones get new months appended, nothing is
committed when there is nothing new:

```bash
python tools/nc_catalog.py example-config > catalogs.json    # then edit
python tools/nc_catalog.py run --config catalogs.json               # all catalogs
python tools/nc_catalog.py run --config catalogs.json ecda          # just one
python tools/nc_catalog.py run --config catalogs.json --check-only  # checks, no writes
python tools/nc_catalog.py run --config catalogs.json --force       # rebuild from scratch
```

```json
{
  "output_root": "/home/jlz/virtualizarr",
  "catalogs": [
    { "id": "ecda", "title": "ECDA reanalysis",
      "roots": ["/data/4/zappalaj/ecda_reanalysis/mo"],
      "vars": null, "pattern": null, "var_from": "filename", "parser": "auto", "keep_going": false }
  ]
}
```

Each catalog lands in `<output_root>/<id>/` (`store/`, `catalog.json`,
`catalog.log`), and `run` prints the matching `datasets.json` entries at the
end. Progress is shown live on a terminal (per-file bar with ETA; warnings
and errors print above it) and as periodic log lines when the output is
redirected, e.g. inside a batch job. `catalog.log` keeps the full record.

What the checks cover (WARNING = catalogued anyway, ERROR = file skipped;
`build` stops on errors unless `--keep-going`):

* filename doesn't match the pattern (default
  `<realm>.YYYYMM-YYYYMM.<var>.nc`; change with `--pattern`, or take the
  variable from the directory name with `--var-from dir`)
* unreadable or **truncated** files (a truncated netCDF-3 file opens fine
  and silently reads as fill values — the tool checks the size against the
  layout)
* time values outside the month the filename claims; duplicate months;
  **missing months** are listed
* grid (lat/lon) differing between files of a variable; dimension order;
  dtype, units, long_name, fill value or calendar changing between files
* file format (netCDF-3 vs netCDF-4/HDF5, either works; mixed collections
  need both parsers), descending latitudes, -180..180 longitudes (both are
  normalised by the viewer)

The catalog records the source directories, so `update` needs no arguments.
Because the store only holds references, the `.nc` files must stay where
they were when the catalog was built (moving them → rebuild with `--force`).

### On the HPC

Everything runs as a normal user process; no root, no database. A typical
setup with the viewer on a login/compute node and the browser on your
laptop:

```bash
# on the HPC (once): environment + catalog
conda create -n windy python=3.12 && conda activate windy
pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -r requirements.txt
python tools/nc_catalog.py build /data/4/.../ecda_reanalysis/mo --out ~/catalogs/ecda --id ecda

# on the HPC: serve (bind to localhost; reach it through the tunnel)
VIEWER_HOST=127.0.0.1 VIEWER_PORT=8601 python windy_viewer/server.py

# on your laptop
ssh -N -L 8601:localhost:8601 hpc-node      # then open http://localhost:8601
```

The viewer can also sit behind a reverse proxy under a path prefix
(e.g. `https://host/ahd-dev/` → the server's `/`): the frontend derives the
prefix from the page URL and makes every API call relative to it, so no
server-side configuration is needed as long as the proxy strips the prefix.

Basemap tiles are fetched by the *browser*, so they work over the tunnel
even if the HPC node has no internet. ArrayLake datasets and SPEAK (LLM
APIs, the RAG service) need outbound HTTPS from the node; without it leave
those out of `datasets.json` / `.env` and everything else still works.
The field cache (`windy_viewer/data/cache`) is per checkout; set
`VIEWER_CACHE_MAX_GB` to bound it.

## Testing without the HPC data

```bash
python tools/make_mock_ecda.py                       # 76 fake months of t_surf, netCDF-3 and netCDF-4 copies
python tools/nc_catalog.py build mock_data/ecda_netcdf3/mo --out catalogs/ecda_nc3 --id ecda
# add {"id": "ecda", "type": "catalog", "path": "catalogs/ecda_nc3"} to datasets.json
```

`make_mock_ecda.py --vars t_surf precip u_ref v_ref slp` adds more FMS
variables (which also exercises the wind animation); `--drop 202301
--truncate 202302` produces a broken set for trying the checks.

## Run with podman

```bash
podman build -t windy-viewer .
podman run --rm -p 8601:8601 --env-file .env -v $PWD/datasets.json:/app/datasets.json:ro windy-viewer
```

For catalog datasets mount the store and the data directories at the same
paths the catalog recorded (`catalog.json: roots`).

## Keys/Secrets

`ARRAYLAKE_TOKEN` (only for ArrayLake datasets), `GEMINI_API_KEY` /
`ANTHROPIC_API_KEY` (optional, enable SPEAK), `CARTO_API_KEY` (optional;
without it CARTO watermarks the basemap tiles — free key at
https://carto.com/basemaps/apikey/). Loaded from `.env` at the repo root
(git-ignored); only `.env.example` is committed.
