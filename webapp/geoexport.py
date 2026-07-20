"""
geoexport — builds the two GeoJSON layers the dashboard offers for download:

  build_snapshot_geojson(stores)  -> "current shapes" layer (shapes.geojson)
  build_changes_geojson(changes)  -> "what changed" layer (changes.geojson),
                                      the primary deliverable — QGIS can
                                      symbolize categorically straight off
                                      each feature's "color" property.
"""

COLORS = {
    "NEW": "blue",
    "EXPANDED": "green",
    "REMOVED": "red",
    "SHRUNK": "orange",
    "BOUNDARY_MODIFIED": "yellow",
    "SPLIT": "purple",
    "MERGE": "black",
    "ABSORBED_NEIGHBOR": "magenta",
    "CRS_MISMATCH": "gray",
}
# RGBA used by qgis_builder.py's categorized renderer — same mapping, QGIS form.
COLOR_RGBA = {
    "NEW": "51,102,204,255",
    "EXPANDED": "40,167,69,255",
    "REMOVED": "220,53,69,255",
    "SHRUNK": "253,126,20,255",
    "BOUNDARY_MODIFIED": "255,193,7,255",
    "SPLIT": "111,66,193,255",
    "MERGE": "20,20,20,255",
    "ABSORBED_NEIGHBOR": "214,51,132,255",
    "CRS_MISMATCH": "128,128,128,255",
}


def _rawtype(store: dict) -> str | None:
    t = (store.get("raw") or {}).get("type")
    if t is None:
        return None
    if isinstance(t, list):
        return ", ".join((x.get("name") if isinstance(x, dict) else x) for x in t)
    return str(t)


def build_snapshot_geojson(stores: list[dict]) -> dict:
    features = []
    without_shape = 0
    for s in stores:
        geometry = s.get("geometry")
        if not geometry:
            without_shape += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "name": s.get("name"),
                "unit": s.get("location_in_outlet"),
                "floor": s.get("floor"),
                "status": s.get("status"),
                "category": s.get("category"),
                "type": _rawtype(s),
                "geometry_crs": s.get("geometry_crs"),
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "stores_without_shape": without_shape,
    }


def build_changes_geojson(changes: list[dict]) -> dict:
    features = []
    for c in changes:
        geometry = c.get("geometry")
        if not geometry:
            continue  # SPLIT/MERGE carry name lists, not a single geometry — properties-only info
        props = {k: v for k, v in c.items() if k != "geometry"}
        props["color"] = COLORS.get(c["change"], "gray")
        features.append({"type": "Feature", "geometry": geometry, "properties": props})
    return {
        "type": "FeatureCollection",
        "features": features,
        "all_changes": changes,   # includes SPLIT/MERGE entries with no single geometry
    }
