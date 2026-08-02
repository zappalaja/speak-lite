"""Climatology map — spatial map of a long-term (e.g. seasonal or annual) mean
for one variable over a region. Use when the user asks for a map, spatial
pattern, or climatology (ArrayLake backend)."""
from arraylake import Client
import xarray as xr
import matplotlib.pyplot as plt

repo = Client().get_repo("GFDL/noaa-gfdl-spear-large-ensembles-pds")
session = repo.readonly_session(branch="main")
ds = xr.open_zarr(session.store, group="historical/Amon", consolidated=False)

# Long-term mean over the chosen window, single member
da = ds["tas"].sel(member_id="r1i1p1f1")
da = da.sel(time=slice("1981-01", "2010-12"))
da = da.sel(lat=slice(20, 55), lon=slice(230, 300))  # lon is 0-360
clim = da.mean(dim="time") - 273.15  # K -> degC

fig, ax = plt.subplots(figsize=(10, 6))
clim.plot(ax=ax, cmap="RdBu_r", cbar_kwargs={"label": "2m air temperature (degC)"})
ax.set_title("1981-2010 mean 2m air temperature")
plt.tight_layout()
plt.show()
