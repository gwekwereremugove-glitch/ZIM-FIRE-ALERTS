#!/usr/bin/env python3
"""
One-off helper: turns your ward-level Districts.shp (1,752 wards, 29 MB) into a small
district-level GeoJSON (63 districts, ~1-2 MB) that loads fast on GitHub Actions.

Usage (on your computer, after `pip install geopandas pyogrio`):
    python prepare_districts.py data/Districts.shp data/districts_simplified.geojson
Then commit data/districts_simplified.geojson (fire_alerts.py picks it up automatically)
and you may delete the big Districts.* files from the repo.
"""
import sys
import geopandas as gpd

src = sys.argv[1] if len(sys.argv) > 1 else "data/Districts.shp"
dst = sys.argv[2] if len(sys.argv) > 2 else "data/districts_simplified.geojson"

g = gpd.read_file(src).to_crs(4326)
g["geometry"] = g.geometry.make_valid()
g["PROVINCE"] = g["PROVINCE"].str.replace("_", " ").str.strip()
g["DISTRICT"] = g["DISTRICT"].str.replace("_", " ").str.strip()
d = g.dissolve(by=["PROVINCE", "DISTRICT"], as_index=False)[["PROVINCE", "DISTRICT", "geometry"]]
d["geometry"] = d.geometry.simplify(0.0005, preserve_topology=True)   # ~50 m, irrelevant for 375 m pixels
d.to_file(dst, driver="GeoJSON")
print(f"{len(g)} wards -> {len(d)} districts, {d.PROVINCE.nunique()} provinces -> {dst}")
