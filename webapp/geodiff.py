"""
geodiff — compares two geometry snapshots (see geometry_store.py) and
classifies what changed about each store's *footprint*, not just its name or
status (mallcore.diff_stores already covers that tabular diff; this module is
additive to it, not a replacement).

Two-pass matching, per a real GIS change-detection design:
  Pass 1 (identity): match stores by id, falling back to a normalized name
    (mirrors reconcile.py's _norm()). geometry_hash lets most stores skip
    Shapely entirely (hash match = definitely unchanged).
  Pass 2 (spatial): whatever's left unmatched after pass 1 gets compared by
    geometric overlap, not id — this is what catches a SPLIT/MERGE where the
    provider assigned the new pieces different ids (id-only matching would
    just report the old store "removed" and two new ones "added").

Every comparison is CRS-guarded: a store is never geometrically compared
across different geometry_crs values (e.g. EPSG:4326 vs SVG_PIXEL) — those
are different spaces and a coordinate delta between them is meaningless.
"""

import re

from shapely.geometry import shape as _shape

GEOMETRY_TOLERANCE = 0.05   # equals_exact tolerance: absorbs export jitter, not real moves
EXPAND_THRESHOLD_PCT = 10.0    # > this area increase -> EXPANDED
SHRINK_THRESHOLD_PCT = -10.0   # < this area decrease -> SHRUNK
BOUNDARY_THRESHOLD_PCT = 5.0   # area change within +-5% but shape differs -> BOUNDARY_MODIFIED

SEVERITY = {
    "NEW": "Medium",
    "REMOVED": "Medium",
    "BOUNDARY_MODIFIED": "Medium",
    "EXPANDED": "High",
    "SHRUNK": "High",
    "SPLIT": "Critical",
    "MERGE": "Critical",
    "ABSORBED_NEIGHBOR": "Critical",
    "CRS_MISMATCH": "Medium",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _key(store: dict) -> str:
    return store.get("id") or _norm(store.get("name"))


def _area_pct_change(old_g, new_g) -> float:
    if old_g.area == 0:
        return 0.0
    return (new_g.area - old_g.area) / old_g.area * 100


def _classify_area_change(old_g, new_g) -> str:
    pct = _area_pct_change(old_g, new_g)
    if pct > EXPAND_THRESHOLD_PCT:
        return "EXPANDED"
    if pct < SHRINK_THRESHOLD_PCT:
        return "SHRUNK"
    return "BOUNDARY_MODIFIED"


def _change(change: str, **fields) -> dict:
    return {"change": change, "severity": SEVERITY[change], **fields}


def diff_snapshots(old: dict | None, new: dict) -> list[dict]:
    """old/new are geometry_store snapshot docs ({"stores": [...]}); old may
    be None (first run for this venue -> nothing to diff, no changes)."""
    if old is None:
        return []

    old_by_key = {_key(s): s for s in old.get("stores", []) if s.get("geometry")}
    new_by_key = {_key(s): s for s in new.get("stores", []) if s.get("geometry")}

    changes: list[dict] = []
    unmatched_old_keys = set(old_by_key) - set(new_by_key)
    unmatched_new_keys = set(new_by_key) - set(old_by_key)

    # --- Pass 1: identity comparison -------------------------------------
    for key in set(old_by_key) & set(new_by_key):
        old_s, new_s = old_by_key[key], new_by_key[key]

        if old_s.get("geometry_crs") != new_s.get("geometry_crs"):
            # Different spaces (e.g. provider swap) -> not comparable. Report
            # it rather than silently skipping, but don't attempt geometry math.
            changes.append(_change(
                "CRS_MISMATCH", store=new_s.get("name"),
                old_crs=old_s.get("geometry_crs"), new_crs=new_s.get("geometry_crs"),
            ))
            continue

        if old_s.get("geometry_hash") and old_s["geometry_hash"] == new_s.get("geometry_hash"):
            continue  # definitely unchanged — no Shapely call needed

        old_g, new_g = _shape(old_s["geometry"]), _shape(new_s["geometry"])
        if old_g.equals_exact(new_g, GEOMETRY_TOLERANCE):
            continue  # hash differed (e.g. vertex order) but shape is the same within tolerance

        change_type = _classify_area_change(old_g, new_g)
        overlap_pct = (old_g.intersection(new_g).area / old_g.area * 100) if old_g.area else 0.0

        # A grown store that now overlaps a DIFFERENT store's old (and now
        # vacant) footprint absorbed that neighbor's unit — a more specific
        # and more useful signal than a plain EXPANDED, and it also means
        # that neighbor shouldn't separately show up as REMOVED below.
        absorbed = None
        if change_type == "EXPANDED":
            for ok in list(unmatched_old_keys):
                neighbor = old_by_key[ok]
                if neighbor.get("geometry_crs") != new_s.get("geometry_crs"):
                    continue
                if _shape(neighbor["geometry"]).intersects(new_g):
                    absorbed = neighbor
                    unmatched_old_keys.discard(ok)
                    break

        if absorbed:
            changes.append(_change(
                "ABSORBED_NEIGHBOR", store=new_s.get("name"),
                absorbed_unit=absorbed.get("id"), absorbed_name=absorbed.get("name"),
                old_area=old_g.area, new_area=new_g.area,
                area_change_percent=round(_area_pct_change(old_g, new_g), 1),
                geometry=new_s["geometry"],
            ))
        else:
            changes.append(_change(
                change_type, store=new_s.get("name"),
                old_area=old_g.area, new_area=new_g.area,
                area_change_percent=round(_area_pct_change(old_g, new_g), 1),
                overlap_percent=round(overlap_pct, 1),
                geometry=new_s["geometry"],
            ))

    # --- Pass 2: spatial comparison over what pass 1 couldn't match ------
    old_leftover = {k: old_by_key[k] for k in unmatched_old_keys}
    new_leftover = {k: new_by_key[k] for k in unmatched_new_keys}
    old_geoms = {k: _shape(s["geometry"]) for k, s in old_leftover.items()}
    new_geoms = {k: _shape(s["geometry"]) for k, s in new_leftover.items()}

    consumed_old, consumed_new = set(), set()

    for ok, og in old_geoms.items():
        if ok in consumed_old:
            continue
        # Only compare within the same CRS space.
        overlapping = [nk for nk, ng in new_geoms.items()
                       if nk not in consumed_new
                       and old_leftover[ok].get("geometry_crs") == new_leftover[nk].get("geometry_crs")
                       and og.intersects(ng)]
        if len(overlapping) > 1:
            changes.append(_change(
                "SPLIT", old=old_leftover[ok].get("name"),
                new=[new_leftover[nk].get("name") for nk in overlapping],
            ))
            consumed_old.add(ok)
            consumed_new.update(overlapping)

    for nk, ng in new_geoms.items():
        if nk in consumed_new:
            continue
        overlapping = [ok for ok, og in old_geoms.items()
                       if ok not in consumed_old
                       and old_leftover[ok].get("geometry_crs") == new_leftover[nk].get("geometry_crs")
                       and og.intersects(ng)]
        if len(overlapping) > 1:
            changes.append(_change(
                "MERGE", old=[old_leftover[ok].get("name") for ok in overlapping],
                new=new_leftover[nk].get("name"),
            ))
            consumed_new.add(nk)
            consumed_old.update(overlapping)

    # Absorbed-neighbor: an unmatched new polygon overlaps a *different*
    # store's now-vacant old polygon (one-to-one overlap, not the >1 case
    # above, which SPLIT/MERGE already claimed).
    for nk, ng in new_geoms.items():
        if nk in consumed_new:
            continue
        for ok, og in old_geoms.items():
            if ok in consumed_old or ok == nk:
                continue
            if old_leftover[ok].get("geometry_crs") != new_leftover[nk].get("geometry_crs"):
                continue
            if og.intersects(ng):
                changes.append(_change(
                    "ABSORBED_NEIGHBOR", store=new_leftover[nk].get("name"),
                    absorbed_unit=old_leftover[ok].get("id"),
                    geometry=new_leftover[nk]["geometry"],
                ))
                consumed_new.add(nk)
                consumed_old.add(ok)
                break

    for ok, s in old_leftover.items():
        if ok not in consumed_old:
            changes.append(_change("REMOVED", store=s.get("name"), geometry=s.get("geometry")))
    for nk, s in new_leftover.items():
        if nk not in consumed_new:
            changes.append(_change("NEW", store=s.get("name"), geometry=s.get("geometry")))

    return changes
