"""
qgis_builder — packages a run's GeoJSON layers into a ready-to-open QGIS
project (.qgz), so "download the data" becomes "one click -> QGIS opens
showing exactly what changed."

Deliberately does NOT use the `qgis.core` (PyQGIS) API: those bindings ship
only inside a full QGIS Desktop/Server install (there is no pip-installable
`qgis` package), which isn't present in this sandbox and won't be present on
a plain Render web service either. A .qgz is just a zip archive containing a
QGIS project XML (.qgs) plus, optionally, the data files it references —
that format is documented and stable, so this module writes it directly.
This is the same kind of deliberate substitution as reconcile.py's CSV
export: no heavy/unavailable dependency, same end result for the user.

Caveat (disclosed, not silently assumed): this was built and structurally
validated (well-formed XML, valid zip, both layers present with the intended
categorized renderer) but never opened in a real QGIS Desktop instance,
since none is installed anywhere in this environment. If a generated project
doesn't render exactly as expected, the categorized-renderer XML below is
the first place to check.
"""

import io
import json
import uuid
import zipfile
from xml.sax.saxutils import escape as _xesc

import geoexport

QGIS_VERSION = "3.34.0"


def _category_xml(value: str, symbol_idx: int, label: str) -> str:
    return (f'<category symbol="{symbol_idx}" value="{_xesc(str(value))}" '
            f'label="{_xesc(label)}" render="true"/>')


def _fill_symbol_xml(symbol_idx: int, rgba: str) -> str:
    return f"""
    <symbol type="fill" name="{symbol_idx}" clip_to_extent="1" force_rhr="0">
      <layer class="SimpleFill" enabled="1" pass="0" locked="0">
        <Option type="Map">
          <Option type="QString" name="color" value="{rgba}"/>
          <Option type="QString" name="outline_color" value="35,35,35,255"/>
          <Option type="QString" name="outline_width" value="0.26"/>
          <Option type="QString" name="style" value="solid"/>
        </Option>
      </layer>
    </symbol>"""


def _categorized_renderer_xml(attr: str, categories: list[tuple[str, str]]) -> str:
    """categories: list of (value, rgba) pairs. Adds a grey catch-all for any
    value not in the list (QGIS's own 'else' category) so an unexpected value
    doesn't render invisibly."""
    symbols = []
    cat_entries = []
    for i, (value, rgba) in enumerate(categories):
        symbols.append(_fill_symbol_xml(i, rgba))
        cat_entries.append(_category_xml(value, i, value))
    else_idx = len(categories)
    symbols.append(_fill_symbol_xml(else_idx, "170,170,170,255"))
    cat_entries.append(f'<category symbol="{else_idx}" value="" label="Other" render="true"/>')

    return f"""
    <renderer-v2 attr="{_xesc(attr)}" type="categorizedSymbol" forceraster="0" enableorderby="0">
      <categories>
        {''.join(cat_entries)}
      </categories>
      <symbols>
        {''.join(symbols)}
      </symbols>
    </renderer-v2>"""


def _label_settings_xml(field: str) -> str:
    """Minimal PAL labeling config: label each feature with `field`."""
    return f"""
    <labeling type="simple">
      <settings calloutType="simple">
        <text-style fieldName="{_xesc(field)}" fontSize="8" fontSizeUnit="Point">
          <text-buffer bufferDraw="1" bufferSize="1" bufferSizeUnits="MM"/>
        </text-style>
        <placement placement="0"/>
      </settings>
    </labeling>"""


def _maplayer_xml(layer_id: str, name: str, datasource: str, renderer_xml: str, labeling_xml: str = "") -> str:
    return f"""
  <maplayer type="vector" geometry="Polygon" simplifyDrawingTol="1" wkbType="Polygon">
    <id>{layer_id}</id>
    <datasource>{_xesc(datasource)}</datasource>
    <layername>{_xesc(name)}</layername>
    <provider encoding="UTF-8">ogr</provider>
    <srs>
      <spatialrefsys>
        <authid>EPSG:4326</authid>
      </spatialrefsys>
    </srs>
    {renderer_xml}
    {labeling_xml}
  </maplayer>"""


def _layer_tree_entry(layer_id: str, name: str) -> str:
    return f'<layer-tree-layer providerKey="ogr" name="{_xesc(name)}" id="{layer_id}" checked="Qt::Checked"/>'


def _build_qgs_xml(venue_name: str, stores_layer_id: str, changes_layer_id: str) -> str:
    stores_categories = [
        ("Open", "40,167,69,220"),
        ("Coming Soon", "255,193,7,220"),
        ("Not in data", "220,53,69,220"),
    ]
    changes_categories = [(k, v) for k, v in geoexport.COLOR_RGBA.items()]

    stores_layer = _maplayer_xml(
        stores_layer_id, "Current Stores", "./shapes.geojson",
        _categorized_renderer_xml("status", stores_categories),
        _label_settings_xml("name"),
    )
    changes_layer = _maplayer_xml(
        changes_layer_id, "Floorplan Changes", "./changes.geojson",
        _categorized_renderer_xml("change", changes_categories),
        _label_settings_xml("store"),
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<qgis projectname="{_xesc(venue_name)} floorplan monitor" version="{QGIS_VERSION}">
  <title>{_xesc(venue_name)} — floorplan changes</title>
  <projectCrs>
    <spatialrefsys>
      <authid>EPSG:4326</authid>
    </spatialrefsys>
  </projectCrs>
  <projectlayers>
    {stores_layer}
    {changes_layer}
  </projectlayers>
  <layer-tree-group>
    {_layer_tree_entry(changes_layer_id, "Floorplan Changes")}
    {_layer_tree_entry(stores_layer_id, "Current Stores")}
  </layer-tree-group>
  <mapcanvas>
    <destinationsrs>
      <spatialrefsys>
        <authid>EPSG:4326</authid>
      </spatialrefsys>
    </destinationsrs>
  </mapcanvas>
</qgis>
"""


def build_qgz(venue_name: str, shapes_geojson: dict, changes_geojson: dict) -> bytes:
    """Returns the raw bytes of a .qgz (a zip containing project.qgs plus the
    two GeoJSON files it references via relative path) — self-contained, so
    opening the .qgz in QGIS needs nothing else on disk."""
    stores_layer_id = f"stores_{uuid.uuid4().hex[:12]}"
    changes_layer_id = f"changes_{uuid.uuid4().hex[:12]}"
    qgs_xml = _build_qgs_xml(venue_name, stores_layer_id, changes_layer_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.qgs", qgs_xml)
        zf.writestr("shapes.geojson", json.dumps(shapes_geojson))
        zf.writestr("changes.geojson", json.dumps(changes_geojson))
    return buf.getvalue()
