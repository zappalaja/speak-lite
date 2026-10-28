#!/usr/bin/env python
"""Generate a small mock of the HPC PRISM daily CONUS files for local testing.

Mimics /data/4/zappalaj/prism/historical/day/atmos/day/r0i0p0/v2/<var>/CONUS24thDv3/
    tas_day_prism_historical_r0i0p0_CONUS24thDv3_19910101-20201231.nc
(NetCDF-3 classic, CMIP-style names, gregorian calendar, days since
1901-01-01, -180..180 longitudes, lat/lon/time bounds, scalar height) on a
coarse grid with synthetic data. tas is written as ONE file spanning all
years (like the real one); pr as one file per year, to exercise both.

    python tools/make_mock_prism.py                      # 1991-1993, tas + pr
    python tools/make_mock_prism.py --first 1991 --last 1992 --vars tas

Output: <out>/prism/historical/day/atmos/day/r0i0p0/v2/<var>/CONUS24thDv3/<file>.nc
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cftime
import numpy as np
from netCDF4 import Dataset

TIME_UNITS = "days since 1901-01-01 00:00:00"
CALENDAR = "gregorian"
GRID = "CONUS24thDv3"


def _tas(doy, LAT, LON):
    season = -np.cos(2 * np.pi * (doy - 15) / 365.25)  # coldest mid-January
    return 288 - 0.6 * (LAT - 37) + 12 * season * (1 - 0.3 * np.cos(np.radians(LON + 100))) \
        - 4 * np.cos(np.radians(LON) * 6)


def _pr(doy, LAT, LON):
    wet = 0.5 + 0.5 * np.cos(2 * np.pi * (doy - 200) / 365.25)
    east = np.clip((LON + 100) / 30, 0, 1)
    return (1e-5 * east * (0.5 + wet) * (1 + 0.5 * np.sin(np.radians(LAT) * 9))).astype(np.float32)


VARS = {
    "tas": ("Daily Mean Near-Surface Air Temperature", "K", "air_temperature", _tas, "one"),
    "pr": ("Precipitation", "kg m-2 s-1", "precipitation_flux", _pr, "yearly"),
}


def write_file(path: Path, var: str, days, lat, lon):
    long_name, units, std, gen, _ = VARS[var]
    nums = cftime.date2num(days, TIME_UNITS, CALENDAR)
    LON, LAT = np.meshgrid(lon, lat)
    doy = np.array([d.timetuple().tm_yday for d in days])
    with Dataset(path, "w", format="NETCDF3_CLASSIC") as nc:
        nc.createDimension("lat", lat.size)
        nc.createDimension("bnds", 2)
        nc.createDimension("lon", lon.size)
        nc.createDimension("time", None)
        h = nc.createVariable("height", "f8", ())
        h.setncatts({"standard_name": "height", "long_name": "height", "units": "m", "positive": "up", "axis": "Z"})
        h.assignValue(2.0)
        v = nc.createVariable("lat", "f8", ("lat",))
        v.setncatts({"standard_name": "latitude", "long_name": "Latitude", "units": "degrees_north", "axis": "Y", "bounds": "lat_bnds"})
        v[:] = lat
        dl = lat[1] - lat[0]
        nc.createVariable("lat_bnds", "f8", ("lat", "bnds"))[:] = np.stack([lat - dl / 2, lat + dl / 2], 1)
        v = nc.createVariable("lon", "f8", ("lon",))
        v.setncatts({"standard_name": "longitude", "long_name": "Longitude", "units": "degrees_east", "axis": "X", "bounds": "lon_bnds"})
        v[:] = lon
        dn = lon[1] - lon[0]
        nc.createVariable("lon_bnds", "f8", ("lon", "bnds"))[:] = np.stack([lon - dn / 2, lon + dn / 2], 1)
        fv = nc.createVariable(var, "f4", ("time", "lat", "lon"), fill_value=np.float32(1.0e20))
        fv.setncatts({"standard_name": std, "long_name": long_name, "units": units, "coordinates": "height",
                      "missing_value": np.float32(1.0e20)})
        t = nc.createVariable("time", "f8", ("time",))
        t.setncatts({"standard_name": "time", "long_name": "time", "bounds": "time_bnds", "units": TIME_UNITS,
                     "calendar": CALENDAR, "axis": "T"})
        tb = nc.createVariable("time_bnds", "f8", ("time", "bnds"))
        tb.setncatts({"units": TIME_UNITS})
        for i in range(len(days)):
            field = gen(doy[i], LAT, LON).astype(np.float32)
            field[(LAT > 47) & (LON < -115)] = 1.0e20  # a masked corner, like outside-CONUS cells
            fv[i] = field
            t[i] = nums[i]
            tb[i] = [nums[i], nums[i] + 1]
        nc.setncatts({
            "Conventions": "CF-1.5", "institution": "NOAA-GFDL",
            "gfdl_download_info": "4kmD2 (BIL) from https://ftp.prism.oregonstate.edu/daily/",
            "gfdlesd_idgrid": GRID, "gfdlesd_idlongname": "PRISMv2-AN81d", "gfdlesd_idname": "PRISMv2",
            "gfdlesd_idscenario": "historical",
            "institution_id": "PRISM Climate Group at Oregon State University",
            "title": f"MOCK {long_name} ({GRID})", "variable_id": var, "frequency": "day",
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "mock_data"))
    ap.add_argument("--vars", nargs="+", default=list(VARS), choices=sorted(VARS))
    ap.add_argument("--first", type=int, default=1991)
    ap.add_argument("--last", type=int, default=1993)
    ap.add_argument("--nlat", type=int, default=52)   # real: 621 x 1405 at 1/24 degree
    ap.add_argument("--nlon", type=int, default=118)
    a = ap.parse_args()
    lat = np.linspace(24.1, 49.9, a.nlat)
    lon = np.linspace(-125.0, -66.5, a.nlon)
    root = Path(a.out) / "prism/historical/day/atmos/day/r0i0p0/v2"
    for var in a.vars:
        d = root / var / GRID
        d.mkdir(parents=True, exist_ok=True)
        spans = [(a.first, a.last)] if VARS[var][4] == "one" else [(y, y) for y in range(a.first, a.last + 1)]
        for y0, y1 in spans:
            t0 = cftime.datetime(y0, 1, 1, calendar=CALENDAR)
            t1 = cftime.datetime(y1, 12, 31, calendar=CALENDAR)
            n = int(cftime.date2num(t1, TIME_UNITS, CALENDAR) - cftime.date2num(t0, TIME_UNITS, CALENDAR)) + 1
            days = cftime.num2date(cftime.date2num(t0, TIME_UNITS, CALENDAR) + np.arange(n), TIME_UNITS, CALENDAR)
            name = f"{var}_day_prism_historical_r0i0p0_{GRID}_{y0}0101-{y1}1231.nc"
            write_file(d / name, var, days, lat, lon)
            print(d / name, n, "days")


if __name__ == "__main__":
    main()
