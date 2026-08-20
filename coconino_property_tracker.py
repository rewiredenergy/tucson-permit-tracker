"""
Coconino County Property Tracker
----------------------------------
Builds a per-parcel property profile for Coconino County, Arizona from the
Arizona Department of Water Resources' (ADWR) statewide "Parcel Finder"
MapServer ("General/Parcels/MapServer", layer 2, "Coconino") -- a public
service ADWR itself describes as backing its "Parcel Finder, GFR and CC&N
applications". Confirmed live via direct browser query: 78,368 parcels,
layer description states the source extract is dated November 2023
("U:\\Resources\\Data\\AZParcels\\Coconino\\2023\\November").

WHY THIS COUNTY WAS ORIGINALLY DEFERRED, AND WHY THIS SOURCE FIXES IT: the
county's own most prominent public ArcGIS layer
("Coconino_County_Parcels_Public_View") only exposes 6 bare fields (APN,
account number, situs address/city, shape area/length) -- explicitly no
owner name and no valuation, by the layer's own description. The full
owner/valuation data instead lives behind a Tyler Technologies "EagleWeb"
per-parcel HTML search portal, not a bulk API. This ADWR mirror is a
DIFFERENT, richer source than that public-view layer -- it includes
OWNER_NAME, which the county's own public layer deliberately omits. See
COUNTIES.md for the original deferral note.

IMPORTANT GAPS: like every other ADWR-sourced county in this repo (Graham/
Greenlee/La Paz/Yavapai), this layer has NO mailing address field (only a
situs "site address"/city/zip, frequently blank) and NO valuation fields
at all (no full cash value, assessed value, land/improvement value) and no
sale price/date history. What's present: owner name, situs address
pieces, book/map/parcel/suffix (the assessor's parcel-number components),
acreage, and lat/lon via computed centroid. Data is noticeably staler
than a county running its own live GIS service with owner/valuation data
-- this is the best public option found for Coconino at this time.

COORDINATES: no native LATITUDE/LONGITUDE/X/Y fields on this layer, so
lat/lon has to be derived from parcel geometry.

This originally used returnCentroid=true&outSR=4326 (with
returnGeometry=false), copied from the sibling "Parcels_for_TEST"
service used by Graham/Greenlee/La Paz/Yavapai on the assumption that
this is the same ADWR backend and therefore behaves identically. It is
NOT. Verified live against this exact layer on 2026-08-19: this
MapServer layer does not implement returnCentroid, and -- critically --
does not error when you pass it. It silently ignores the parameter and
returns features containing ONLY an "attributes" key: no "centroid",
no "geometry". The old code then read f.get("centroid") or {}, got an
empty dict, and wrote _LATITUDE/_LONGITUDE = None for every single row
while still marking each row status="enriched". That is the exact
mechanism behind 78,368 Coconino rows sitting at 100% NULL coordinates.

The layer's own metadata corroborates this: advancedQueryCapabilities
reports supportsPagination=true but does NOT contain a
supportsReturningGeometryCentroid key at all. (The prior note here
claimed both were true. They are not; that claim was never checked
against this service.)

Fix: request real polygon geometry (returnGeometry=true, outSR=4326)
and compute the centroid client-side in ring_centroid() below. Verified
live: the layer does return esriGeometryPolygon rings in WGS84 decimal
degrees, e.g. [-111.71096479264996, 35.123110532078236] for a parcel
near Flagstaff. geometryPrecision=6 trims the payload to ~0.1m
resolution, which is far finer than a parcel centroid needs and keeps
the extra bandwidth over the old (broken) attributes-only request
modest.

NOTE: this layer lives under a MapServer (General/Parcels/MapServer/2),
not a FeatureServer like every other tracker in this repo. The /query
endpoint and pagination params are identical, but as above, do NOT
assume MapServer capability parity for geometry/centroid options --
check the layer's advancedQueryCapabilities first.

Below the ~200k-parcel threshold, so no resumable checkpoint table is
needed (simple full re-pull every run, same design as Santa Cruz/Yuma/
Cochise/Navajo/Apache/Gila/Graham/Greenlee/La Paz/Yavapai).

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55 (safety net -- this job
                               normally finishes in a few minutes)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import math

FEATURE_URL = ("https://azwatermaps.azwater.gov/arcgis/rest/services/General/"
               "Parcels/MapServer/2/query")

PAGE_SIZE = 2000  # this layer's maxRecordCount
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the server

OUT_FIELDS = (
    "OBJECTID,ID,COUNTY,APN,BOOK,MAP,PARCEL,SUFFIX,SITE_ADDRESS,SITE_CITY,"
    "SITE_ZIP,OWNER_NAME,URL,ACRES_US"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredCoconinoPropertyTracker/1.0",
    "Accept": "application/json",
}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)
MAX_RUNTIME_MINUTES = float(os.environ.get("MAX_RUNTIME_MINUTES") or 55)

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

_start = time.monotonic()

_HREF_RE = re.compile(r'href="([^"]+)"')


# ---------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------
def to_num(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    # ArcGIS occasionally returns the literal string "NaN" (or "Infinity")
    # for a degenerate/zero-area parcel's computed centroid -- float() parses
    # those "successfully" into a non-finite value, which then breaks
    # PostgREST's strict JSON parser downstream ("Empty or invalid json"),
    # crashing the whole batch upsert. Reject non-finite results here instead.
    return v if math.isfinite(v) else None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


def ring_centroid(rings):
    """Area-weighted centroid of an ArcGIS polygon's ring list.

    Returns (longitude, latitude), or (None, None) if the geometry is
    missing or unusable.

    ArcGIS polygons carry every ring in one flat "rings" list: exterior
    rings wound clockwise, interior rings (holes) counter-clockwise.
    The standard shoelace formula produces a SIGNED area whose sign
    follows that winding, so summing each ring's signed area and signed
    first moments makes holes subtract themselves automatically -- no
    need to classify rings as outer vs. inner. A multi-part parcel
    (several disjoint pieces sharing one APN, common here for split
    ranch/forest parcels) falls out of the same sum as a true
    area-weighted centre rather than the centre of whichever part
    happened to be listed first.

    The math runs directly on lon/lat degrees rather than a projected
    CRS. That is a planar approximation, but at parcel scale the error
    is far below a metre -- meaningless next to the parcel's own size,
    and these coordinates exist to drop a pin on a property.

    Falls back to the plain vertex mean when total area is ~0, which
    covers the degenerate cases the county data does contain: zero-area
    slivers and rings recorded as a single repeated point. Returning
    the vertex mean beats returning NULL -- it is still inside/at the
    parcel -- and avoids the NaN that dividing by zero area would push
    downstream into PostgREST (see to_num()'s isfinite guard).
    """
    if not rings:
        return None, None

    area2 = 0.0   # 2x signed area
    cx = 0.0      # 6x area-weighted x numerator
    cy = 0.0
    vx = vy = 0.0
    vn = 0
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    for ring in rings:
        if not ring or len(ring) < 2:
            continue
        for i in range(len(ring) - 1):
            try:
                x0, y0 = float(ring[i][0]), float(ring[i][1])
                x1, y1 = float(ring[i + 1][0]), float(ring[i + 1][1])
            except (TypeError, ValueError, IndexError):
                continue
            cross = x0 * y1 - x1 * y0
            area2 += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        for pt in ring:
            try:
                px, py = float(pt[0]), float(pt[1])
            except (TypeError, ValueError, IndexError):
                continue
            vx += px
            vy += py
            vn += 1
            minx = min(minx, px)
            maxx = max(maxx, px)
            miny = min(miny, py)
            maxy = max(maxy, py)

    if abs(area2) > 1e-12:
        lon = cx / (3.0 * area2)
        lat = cy / (3.0 * area2)
        # Sanity guard: a valid polygon centroid always lies inside the
        # geometry's own bounding box. A near-degenerate ring (a sliver, or
        # an unclosed arc) can have a tiny-but-nonzero signed area that
        # divides into a wildly out-of-range point, which would land a pin
        # in the wrong county. Reject that and use the vertex mean instead.
        if (math.isfinite(lon) and math.isfinite(lat)
                and minx <= lon <= maxx and miny <= lat <= maxy):
            return lon, lat

    if vn:
        lon, lat = vx / vn, vy / vn
        if math.isfinite(lon) and math.isfinite(lat):
            return lon, lat

    return None, None


def extract_url(value):
    # ADWR's URL field is a raw HTML anchor tag, e.g.
    # '<a href="http://..." target="_blank">Assessor Parcel Search Link</a>'
    v = clean(value)
    if not v:
        return None
    m = _HREF_RE.search(v)
    return m.group(1) if m else v


# ---------------------------------------------------------------
# Bulk ArcGIS MapServer pagination (identical /query shape to FeatureServer)
# ---------------------------------------------------------------
def fetch_page(offset: int) -> list:
    params = {
        "where": "1=1",
        "outFields": OUT_FIELDS,
        # This layer silently ignores returnCentroid (see COORDINATES in the
        # module docstring), so pull real rings and compute the centroid
        # locally. geometryPrecision=6 caps coordinates at ~0.1m resolution.
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "6",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": "APN ASC",
        "f": "json",
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            r = session.get(FEATURE_URL, params=params, timeout=60)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(str(d["error"])[:200])
            out = []
            for f in (d.get("features") or []):
                attrs = f["attributes"]
                lon, lat = ring_centroid((f.get("geometry") or {}).get("rings"))
                attrs["_LONGITUDE"] = lon
                attrs["_LATITUDE"] = lat
                out.append(attrs)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"fetch failed at offset {offset}: {last_err}")


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------

# Every column a row might set, EXCLUDING "parcel" (always present, it's
# the primary key). Every upserted row -- normal or a defensive error
# row -- gets padded to this exact column set: PostgREST's bulk upsert
# rejects a batch whose objects don't all have EXACTLY the same keys
# (PGRST102 "All object keys must match"), the same crash Pima's
# property_info_tracker.py hit in production before that fix was added
# there. jurisdiction defaults to "coconino_county" since the column is
# NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "property_city", "property_zip",
    "owner_name", "book", "map_number", "parcel_number", "suffix",
    "land_size_acres", "latitude", "longitude", "source_url",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "coconino_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("APN")) or clean(a.get("ID"))
    owner_name = clean(a.get("OWNER_NAME"))
    return {
        "parcel": parcel,
        "jurisdiction": "coconino_county",
        "property_address": clean(a.get("SITE_ADDRESS")),
        "property_city": clean(a.get("SITE_CITY")),
        "property_zip": clean(a.get("SITE_ZIP")),
        "owner_name": owner_name,
        "book": clean(a.get("BOOK")),
        "map_number": clean(a.get("MAP")),
        "parcel_number": clean(a.get("PARCEL")),
        "suffix": clean(a.get("SUFFIX")),
        "land_size_acres": to_num(a.get("ACRES_US")),
        "latitude": to_num(a.get("_LATITUDE")),
        "longitude": to_num(a.get("_LONGITUDE")),
        "source_url": extract_url(a.get("URL")),
        "status": "enriched" if owner_name else "no_owner_data",
        "enriched_at": now_iso,
        "updated_at": now_iso,
        "raw": a,
    }


# ---------------------------------------------------------------
# Supabase (same retry-wrapped upsert pattern as the other trackers)
# ---------------------------------------------------------------
def upsert(table: str, rows: list, on_conflict: str = None) -> None:
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        last_err = None
        for attempt in range(1, 6):
            try:
                r = session.post(url, headers=headers, data=json.dumps(batch), timeout=120)
                if r.status_code >= 300:
                    raise RuntimeError(f"Supabase write to {table} failed "
                                       f"({r.status_code}): {r.text[:300]}")
                last_err = None
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_err = e
                wait = 5 * attempt
                print(f"  Supabase write to {table} network error "
                      f"(attempt {attempt}/5): {e} -- retrying in {wait}s")
                time.sleep(wait)
        if last_err is not None:
            raise RuntimeError(f"Supabase write to {table} failed after 5 attempts: {last_err}")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"Run started {now_iso}"
          + (f" -- TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else "")
          + f" -- runtime budget {MAX_RUNTIME_MINUTES} min")

    offset = 0
    processed = 0
    pages = 0

    while True:
        elapsed_min = (time.monotonic() - _start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"Reached runtime budget ({MAX_RUNTIME_MINUTES} min) -- "
                  f"stopping at offset {offset} ({processed} parcels this run).")
            break
        if SAMPLE_LIMIT and processed >= SAMPLE_LIMIT:
            print(f"Reached sample limit ({SAMPLE_LIMIT}) -- stopping.")
            break

        time.sleep(REQUEST_DELAY)
        raw_rows = fetch_page(offset)
        if not raw_rows:
            print(f"Reached the end of the parcel list at offset {offset} -- full sweep complete!")
            break

        rows = []
        for a in raw_rows:
            try:
                rows.append(normalize_row(build_row(a, now_iso)))
            except Exception as e:  # noqa: BLE001
                pid = a.get("APN") or a.get("ID") or "unknown"
                print(f"  warning: row build failed for {pid}: {e}")
                rows.append(normalize_row({
                    "parcel": pid, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("coconino_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run (now at offset {offset})")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
