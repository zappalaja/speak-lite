#!/usr/bin/env python
"""Generate a mock of the HPC ECDA reanalysis directory for local testing.

Writes one NetCDF file per month with the exact names, dimensions,
variables and attributes of the real files
(``atmos.YYYYMM-YYYYMM.<var>.nc``, FMS/fregrid monthly output on a
180 x 288 regular grid, JULIAN calendar, time in days since 1990-01-01)
but with synthetic data. The default matches the real listing: t_surf
for 2020-05 .. 2026-08 (76 files).

    python tools/make_mock_ecda.py                       # both formats
    python tools/make_mock_ecda.py --format netcdf3      # NETCDF3_64BIT only
    python tools/make_mock_ecda.py --vars t_surf precip u_ref v_ref
    python tools/make_mock_ecda.py --drop 202301 --truncate 202302   # exercise the catalog checks

Output: <out>/ecda_<format>/mo/<var>/atmos.YYYYMM-YYYYMM.<var>.nc
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import cftime
import numpy as np
from netCDF4 import Dataset

FORMATS = {"netcdf3": "NETCDF3_64BIT", "netcdf4": "NETCDF4_CLASSIC"}
TIME_UNITS = "days since 1990-01-01 00:00:00"
CALENDAR = "julian"

# FMS atmos_month variables: (long_name, units, valid_range, generator)
# The generator returns a (lat, lon) field in native units for a given
# (year, month) — smooth, physically plausible, with a seasonal cycle.
def _t_surf(y, m, LAT, LON):
    season = np.cos(2 * np.pi * (m - 7) / 12) * np.sign(LAT)  # NH summer in July
    land = 6 * np.sin(np.radians(LON) * 2) * np.cos(np.radians(LAT) * 3)
    trend = 0.02 * (y - 2020)
    return 300 - 45 * np.sin(np.radians(LAT)) ** 2 - 12 * season * np.sin(np.radians(LAT)) ** 2 + land + trend


def _precip(y, m, LAT, LON):
    itcz = 8 + 4 * np.cos(2 * np.pi * (m - 7) / 12)  # ITCZ wanders north in NH summer
    base = 8e-5 * np.exp(-((LAT - itcz) / 8) ** 2) + 2e-5 * np.exp(-((np.abs(LAT) - 50) / 12) ** 2)
    return np.clip(base * (1 + 0.3 * np.sin(np.radians(LON) * 3)), 0, None)


def _u_ref(y, m, LAT, LON):
    return -6 * np.cos(np.radians(LAT) * 3) + 2 * np.sin(np.radians(LON) * 2)


def _v_ref(y, m, LAT, LON):
    return 3 * np.sin(np.radians(LAT) * 2) * np.cos(np.radians(LON))


def _slp(y, m, LAT, LON):
    return 101325 + 1200 * np.cos(np.radians(LAT) * 3) + 400 * np.sin(np.radians(LON) * 2)


VARS = {
    "t_surf": ("surface temperature", "deg_k", (100.0, 400.0), _t_surf),
    "precip": ("Total precipitation rate", "kg/m2/s", (-1.0, 0.1), _precip),
    "u_ref": ("zonal wind component at 10 m", "m/s", (-400.0, 400.0), _u_ref),
    "v_ref": ("meridional wind component at 10 m", "m/s", (-400.0, 400.0), _v_ref),
    "slp": ("sea level pressure", "Pa", (10000.0, 120000.0), _slp),
}


def month_range(first: str, last: str) -> list[tuple[int, int]]:
    y, m = int(first[:4]), int(first[4:])
    y1, m1 = int(last[:4]), int(last[4:])
    out = []
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def write_month(path: Path, var: str, y: int, m: int, fmt: str, lat, lon, lat_bnds, lon_bnds):
    long_name, units, vrange, gen = VARS[var]
    start = cftime.date2num(cftime.datetime(y, m, 1, calendar=CALENDAR), TIME_UNITS, CALENDAR)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = cftime.date2num(cftime.datetime(ny, nm, 1, calendar=CALENDAR), TIME_UNITS, CALENDAR)
    mid = (start + end) / 2
    LON, LAT = np.meshgrid(lon, lat)
    field = gen(y, m, LAT, LON).astype(np.float32)[None]

    with Dataset(path, "w", format=FORMATS[fmt]) as nc:
        nc.createDimension("time", None)
        nc.createDimension("bnds", 2)
        nc.createDimension("lat", lat.size)
        nc.createDimension("lon", lon.size)

        def dvar(name, dims, **attrs):
            v = nc.createVariable(name, "f8", dims, fill_value=attrs.pop("_FillValue", None))
            v.setncatts(attrs)
            return v

        v = dvar("average_DT", ("time",), long_name="Length of average period", units="days",
                 missing_value=1.0e20, _FillValue=1.0e20)
        v[:] = [end - start]
        v = dvar("average_T1", ("time",), long_name="Start time for average period", units=TIME_UNITS,
                 missing_value=1.0e20, _FillValue=1.0e20)
        v[:] = [start]
        v = dvar("average_T2", ("time",), long_name="End time for average period", units=TIME_UNITS,
                 missing_value=1.0e20, _FillValue=1.0e20)
        v[:] = [end]
        v = dvar("bnds", ("bnds",), long_name="vertex number", units="none", cartesian_axis="N")
        v[:] = [1.0, 2.0]
        v = dvar("lat", ("lat",), long_name="latitude", units="degrees_N", axis="Y", bounds="lat_bnds")
        v[:] = lat
        v = dvar("lat_bnds", ("lat", "bnds"), long_name="latitude bounds", units="degrees_N", axis="Y")
        v[:] = lat_bnds
        v = dvar("lon", ("lon",), long_name="longitude", units="degrees_E", axis="X", bounds="lon_bnds")
        v[:] = lon
        v = dvar("lon_bnds", ("lon", "bnds"), long_name="longitude bounds", units="degrees_E", axis="X")
        v[:] = lon_bnds

        fv = nc.createVariable(var, "f4", ("time", "lat", "lon"), fill_value=np.float32(1.0e20))
        fv.setncatts({
            "long_name": long_name, "units": units,
            "valid_range": np.array(vrange, dtype=np.float32),
            "missing_value": np.float32(1.0e20),
            "cell_methods": "time: mean",
            "time_avg_info": "average_T1,average_T2,average_DT",
            "interp_method": "conserve_order2",
        })
        fv[:] = field

        v = dvar("time", ("time",), long_name="time", units=TIME_UNITS, cartesian_axis="T",
                 calendar_type="JULIAN", calendar="JULIAN", bounds="time_bnds")
        v[:] = [mid]
        v = dvar("time_bnds", ("time", "bnds"), long_name="time axis boundaries", units="days",
                 missing_value=1.0e20, _FillValue=1.0e20)
        v[:] = [[start, end]]

        nc.setncatts({
            "filename": path.name,
            "title": "SPEAR_c96_o1_ECDA_J11",
            "grid_type": "regular",
            "grid_tile": "N/A",
            "NCO": '"4.5.4"',
            "nco_openmp_thread_number": np.int32(1),
            "comment": "pressure level interpolator, version 3.0, precision=double",
            "history": (f"fregrid --standard_dimension --input_mosaic C96_mosaic.nc --input_file "
                        f"{y:04d}{m:02d}01.atmos_month --interp_method conserve_order2 "
                        "--remap_file .fregrid_remap_file_288_by_180.nc --nlon 288 --nlat 180 "
                        "--scalar_field (**please see the field list in this file**) --output_file out.nc"),
            "code_version": "$Name: bronx-10_performance_z1l $",
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "mock_data"))
    ap.add_argument("--format", choices=["netcdf3", "netcdf4", "both"], default="both")
    ap.add_argument("--vars", nargs="+", default=["t_surf"], choices=sorted(VARS))
    ap.add_argument("--first", default="202005")
    ap.add_argument("--last", default="202608")
    ap.add_argument("--drop", nargs="*", default=[], metavar="YYYYMM",
                    help="months to leave out (to test the missing-month check)")
    ap.add_argument("--truncate", nargs="*", default=[], metavar="YYYYMM",
                    help="months whose file is cut short (to test the unreadable-file check)")
    args = ap.parse_args()

    nlat, nlon = 180, 288
    lat = -90 + (np.arange(nlat) + 0.5) * (180 / nlat)
    lon = (np.arange(nlon) + 0.5) * (360 / nlon)
    lat_bnds = np.stack([lat - 0.5, lat + 0.5], axis=1)
    lon_bnds = np.stack([lon - 0.625, lon + 0.625], axis=1)

    formats = ["netcdf3", "netcdf4"] if args.format == "both" else [args.format]
    for fmt in formats:
        for var in args.vars:
            d = Path(args.out) / f"ecda_{fmt}" / "mo" / var
            d.mkdir(parents=True, exist_ok=True)
            n = 0
            for y, m in month_range(args.first, args.last):
                tag = f"{y:04d}{m:02d}"
                if tag in args.drop:
                    continue
                p = d / f"atmos.{tag}-{tag}.{var}.nc"
                write_month(p, var, y, m, fmt, lat, lon, lat_bnds, lon_bnds)
                if tag in args.truncate:
                    data = p.read_bytes()
                    p.write_bytes(data[: len(data) // 3])
                n += 1
            print(f"{d}: {n} files ({FORMATS[fmt]})")


if __name__ == "__main__":
    main()
