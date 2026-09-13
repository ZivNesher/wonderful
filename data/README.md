# Data files

Static, bundled snapshots — nothing here is fetched live (see `src/retrieval.py` for the one live BTS call).

- **airports_us.csv** — 646 US airports with scheduled commercial service. From [OurAirports](https://ourairports.com/data/).
- **airports_world.csv** — coordinates for any airport worldwide, used for long-haul distance calculations. From OurAirports.
- **routes.csv** — nonstop route pairs. From [OpenFlights](https://openflights.org/data.php) `routes.dat`.
- **runways_us.csv** — runway counts/lengths for the US whitelist. From OurAirports `runways.csv`.
