"""
Gila County Property Tracker
----------------------------------
Builds a per-parcel property profile for Gila County, Arizona (~33k
parcels) from ONE public bulk ArcGIS FeatureServer layer ("ParcelService")
published in the county's own ArcGIS Online org (gilacountyaz.maps.arcgis.com)
-- confirmed as the layer backing the county's own "Assessor Parcel Viewer"
web app. A legacy ArcGIS Server at gis.gilacountyaz.gov hosts MapServer
copies of the same underlying data, but that MapServer does NOT honor
returnCentroid (silently ignores it) -- use the services1.arcgis.com
FeatureServer below specifically because it does.

IMPORTANT GAP: this layer has NO valuation fields at all (no full cash
value, assessed value, land/improvement value) and no sale price/date
history -- checked every parcel-related service in the county's ArcGIS
org, none expose it. What's present: owner name(s), mailing address
(split into address/city/state/zip), situs address, acreage, and lat/lon
via computed centroid.

Owner/address data freshness caveat: the county appears to publish
several parallel copies of this roster with inconsistent freshness (a
legacy MapServer snapshot showed stale ~2016-2018 data for the same
parcel that's current here) -- this FeatureServer is the one wired into
the live public viewer and is the freshest available, but don't assume
GISDate is a reliable last-updated indicator (it looks like a legacy
digitization timestamp).

COORDINATES: no native LATITUDE/LONGITUDE/X/Y fields. Uses
returnCentroid=true&outSR=4326 (with returnGeometry=false) -- verified
live against this exact layer, e.g. {"x": -111.315..., "y": 34.245...}
for parcel 30474001, correctly inside Gila County.

Gila County includes portions of the San Carlos Apache and Fort Apache
reservations. Trust/reservation land is not county-assessed and is
excluded from this layer entirely (the source's own JURISDICTION field
only ever contains "GILA") -- expect gaps over reservation areas, not a
scraping bug.

Below the ~200k-parcel threshold, so no resumable checkpoint table is
needed (simple full re-pull every run, same design as Santa Cruz/Yuma/
Cochise/Navajo/Apache).

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55 (safety net -- this job
                               normally finishes in a few minutes)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FEATURE_URL = ("https://services1.arcgis.com/PEZ16KoOWXoETFQ5/arcgis/rest/"
               "services/ParcelService/FeatureServer/0/query")

PAGE_SIZE = 1000  # this layer's maxRecordCount is 1000, not the usual 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the server

OUT_FIELDS = (
    "APN,LAPN,Owner1,Owner2,MAILADDRESS1,MAILADDRESS2,MAILCITY,MAILSTATE,"
    "MAILZIPCODE,ADDRESS,LANDTYPE,LANDACRES,LANDSF,ACCOUNTNO"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredGilaPropertyTracker/1.0",
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
        return float(s)
    except ValueError:
        return None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination
# ---------------------------------------------------------------
def fetch_page(offset: int) -> list:
    params = {
        "where": "1=1",
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "returnCentroid": "true",  # server-computed lat/lon; layer has no native lat/lon fields
        "outSR": "4326",
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
                centroid = f.get("centroid") or {}
                attrs["_LONGITUDE"] = centroid.get("x")
                attrs["_LATITUDE"] = centroid.get("y")
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
# there. jurisdiction defaults to "gila_county" since the column is
# NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "owner_name", "owner_name_2",
    "account_number",
    "mailing_address_1", "mailing_address_2", "mailing_city", "mailing_state", "mailing_zip",
    "land_type", "land_size_acres", "land_size_sqft",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "gila_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("APN")) or clean(a.get("LAPN"))
    owner_name = clean(a.get("Owner1"))
    return {
        "parcel": parcel,
        "jurisdiction": "gila_county",
        "property_address": clean(a.get("ADDRESS")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("Owner2")),
        "account_number": clean(a.get("ACCOUNTNO")),
        "mailing_address_1": clean(a.get("MAILADDRESS1")),
        "mailing_address_2": clean(a.get("MAILADDRESS2")),
        "mailing_city": clean(a.get("MAILCITY")),
        "mailing_state": clean(a.get("MAILSTATE")),
        "mailing_zip": clean(a.get("MAILZIPCODE")),
        "land_type": clean(a.get("LANDTYPE")),
        "land_size_acres": to_num(a.get("LANDACRES")),
        "land_size_sqft": to_num(a.get("LANDSF")),
        "latitude": to_num(a.get("_LATITUDE")),
        "longitude": to_num(a.get("_LONGITUDE")),
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
                pid = a.get("APN") or "unknown"
                print(f"  warning: row build failed for {pid}: {e}")
                rows.append(normalize_row({
                    "parcel": pid, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("gila_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run (now at offset {offset})")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
