"""
Graham County Property Tracker
----------------------------------
Builds a per-parcel property profile for Graham County, Arizona (~18.8k
parcels) from the Arizona Department of Water Resources' (ADWR) statewide
"Parcels_for_TEST" FeatureServer, layer 2 ("Graham_Parcels") -- a public
fallback service that hosts one parcel layer per rural AZ county that
doesn't run its own mature public-facing GIS FeatureServer. Confirmed live
via direct browser query: 18,821 parcels, layer description states the
source extract is dated Nov 2023 ("U:\\Resources\\Data\\AZ_Parcels\\Graham").

IMPORTANT GAPS: this ADWR layer has NO mailing address field (only a situs
"site address"/city/zip, frequently blank) and NO valuation fields at all
(no full cash value, assessed value, land/improvement value) and no sale
price/date history. What's present: owner name, situs address pieces,
book/map/parcel/suffix (the assessor's parcel-number components), acreage,
and lat/lon via computed centroid. Data is noticeably staler than a county
running its own live GIS service -- this is the best/only public option
found for Graham, which does not run one.

COORDINATES: no native LATITUDE/LONGITUDE/X/Y fields. Uses
returnCentroid=true&outSR=4326 (with returnGeometry=false) -- verified
live against this exact layer.

Below the ~200k-parcel threshold, so no resumable checkpoint table is
needed (simple full re-pull every run, same design as Santa Cruz/Yuma/
Cochise/Navajo/Apache/Gila).

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

FEATURE_URL = ("https://azwatermaps.azwater.gov/arcgis/rest/services/General/"
               "Parcels_for_TEST/FeatureServer/2/query")

PAGE_SIZE = 2000  # this layer's maxRecordCount
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the server

OUT_FIELDS = (
    "OBJECTID,ID,COUNTY,APN,BOOK,MAP,PARCEL,SUFFIX,SITE_ADDRESS,SITE_CITY,"
    "SITE_ZIP,OWNER_NAME,URL,ACRES_US"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredGrahamPropertyTracker/1.0",
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
        return float(s)
    except ValueError:
        return None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


def extract_url(value):
    # ADWR's URL field is a raw HTML anchor tag, e.g.
    # '<a href="http://..." target="_blank">Assessor Parcel Search Link</a>'
    v = clean(value)
    if not v:
        return None
    m = _HREF_RE.search(v)
    return m.group(1) if m else v


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
# there. jurisdiction defaults to "graham_county" since the column is
# NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "property_city", "property_zip",
    "owner_name", "book", "map_number", "parcel_number", "suffix",
    "land_size_acres", "latitude", "longitude", "source_url",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "graham_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("APN")) or clean(a.get("ID"))
    owner_name = clean(a.get("OWNER_NAME"))
    return {
        "parcel": parcel,
        "jurisdiction": "graham_county",
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
        upsert("graham_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run (now at offset {offset})")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
