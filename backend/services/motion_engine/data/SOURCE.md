# Countries outline dataset — source & attribution

`countries.geojson` — simplified world country outlines (180 features,
GeoJSON, coordinates rounded to 3 decimal places).

- **Source:** `countries.geo.json` from the johan/world.geo.json project
  (https://github.com/johan/world.geo.json), fetched 2026-11-19.
- **Underlying data:** Natural Earth (https://www.naturalearthdata.com),
  1:110m cultural vectors — **public domain**. Natural Earth data is
  free for use in any project, commercial or otherwise, with no
  attribution legally required; this note is kept anyway so the
  provenance of every rendered outline is auditable.
- **Processing applied here:** coordinates rounded to 3 decimals
  (~100 m at the equator — far below the resolution a 1920×1080 map
  graphic can express) and feature properties reduced to the country
  name. No geometry was redrawn, moved or invented.

Usage rule (enforced by `services/motion_engine/geography.py` and the
`territory_map` template): an outline is rendered ONLY when the
requested country exists in this dataset. Countries not in the dataset
are never approximated — the template falls back to caller-supplied
polygons or refuses.
