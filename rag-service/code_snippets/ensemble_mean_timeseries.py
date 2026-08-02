"""Ensemble statistics (mean/spread) time series over a region — use when the
user asks about multiple ensemble members, the ensemble mean, or model spread
(ArrayLake backend)."""
from arraylake import Client
import xarray as xr
import matplotlib.pyplot as plt

repo = Client().get_repo("GFDL/noaa-gfdl-spear-large-ensembles-pds")
session = repo.readonly_session(branch="main")
ds = xr.open_zarr(session.store, group="historical/Amon", consolidated=False)

# Regional mean time series for every ensemble member
da = ds["tas"].sel(time=slice("1990-01", "2014-12"))
da = da.sel(lat=slice(20, 50), lon=slice(230, 300))  # lon is 0-360
da = da.mean(dim=["lat", "lon"]) - 273.15  # K -> degC

ens_mean = da.mean(dim="member_id")
ens_min = da.min(dim="member_id")
ens_max = da.max(dim="member_id")

fig, ax = plt.subplots(figsize=(10, 5))
ax.fill_between(da.time, ens_min, ens_max, alpha=0.3, label="Ensemble spread (min-max)")
ens_mean.plot(ax=ax, color="k", label="Ensemble mean")
ax.set_xlabel("Time")
ax.set_ylabel("2m air temperature (degC)")
ax.legend()
plt.tight_layout()
plt.show()
