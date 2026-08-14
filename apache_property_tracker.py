"""
Apache County Property Tracker
----------------------------------
Builds a per-parcel property profile for Apache County, Arizona (~58k
parcels) from ONE public bulk ArcGIS FeatureServer layer ("Parcel8252025")
published by Apache County's own GIS org (services8.arcgis.com, account
that created the item is an Apache County GIS staffer -- confirmed via
the item matching exactly what the county's own public web map renders,
at arcgis.com/apps/webappviewer/index.html?id=2fdb74d76b734d4c98869038eae12aea).

The org has a long trail of dated one-off parcel exports (Parcel01072025,
Parcel02262025, ... Parcel8252025) -- the county re-uploads a fresh full
extract periodically rather than editing one layer in place. Parcel8252025
is the one actually wired into the live public map (last edited Oct 2025)
and is used here. An older, richer-schema layer ("Parcels" /
"Apache_County_Parcels", last edited Jan 2022) also exists with mailing
address and township/range/section fields, but hasn't been touched in
~4 years -- deliberately NOT used here since its owner/mailing data would
be stale; if that data is ever wanted, treat it as a clearly-flagged
secondary/backup source, not primary.

IMPORTANT GAPS: this layer has NO mailing address field and NO valuation
fields at all (no full cash value, assessed value, land/improvement
value) and no sale price/date history. What's present: owner name, situs
address (terse/inconsistent format, frequently blank on vacant land),
acreage (stored as a STRING on this layer, not numeric -- confirmed via
live sample, e.g. "0.2", "1.89"), and lat/lon via computed centroid.

COORDINATES: no native LATITUDE/LONGITUDE/X/Y fields. Uses
returnCentroid=true&outSR=4326 (with returnGeometry=false) -- verified
live against this exact layer, e.g. {"x": -109.114..., "y": 33.835...}
for parcel 101-73-039, correctly inside Apache County (St. Johns area).

Apache County includes large portions of the Navajo Nation and the Fort
Apache (White Mountain Apache) Reservation. Trust/reservation land is not
county-assessed, so it simply does not appear as rows in this layer --
large geographic gaps over reservation areas are expected, not a bug.

Below the ~200k-parcel threshold, so no resumable checkpoint table is
needed (simple full re-pull every run, same design as Santa Cruz/Yuma/
Cochise/Navajo).

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

FEATURE_URL = ("https://services8.arcgis.com/KyZIQDOsXnGaTxj2/arcgis/rest/"
               "services/Parcel8252025/FeatureServer/0/query")

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the server

OUT_FIELDS = "OBJECTID,PARCEL_NUM,NUMBER,OWNER,SITUS,SIZE"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredApachePropertyTracker/1.0",
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
        "orderByFields": "PARCEL_NUM ASC",
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
# there. jurisdiction defaults to "apache_county" since the column is
# NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "owner_name", "account_number",
    "land_size_acres", "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "apache_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    # A handful of rows in this layer are essentially empty placeholder
    # polygons with PARCEL_NUM (and every other attribute) null -- e.g.
    # OBJECTID 143735, confirmed live. Fall back to a synthetic
    # "apache-oid-<OBJECTID>" key so those rows still satisfy the not-null
    # primary key instead of crashing the whole batch upsert (PostgreSQL
    # 23502), same fallback pattern used by the ADWR-sourced counties.
    parcel = clean(a.get("PARCEL_NUM")) or (
        f"apache-oid-{a['OBJECTID']}" if a.get("OBJECTID") is not None else None
    )
    if not parcel:
        raise ValueError("missing PARCEL_NUM and OBJECTID")
    owner_name = clean(a.get("OWNER"))
    return {
        "parcel": parcel,
        "jurisdiction": "apache_county",
        "property_address": clean(a.get("SITUS")),
        "owner_name": owner_name,
        "account_number": clean(a.get("NUMBER")),
        "land_size_acres": to_num(a.get("SIZE")),
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
                pid = a.get("PARCEL_NUM") or (
                    f"apache-oid-{a['OBJECTID']}" if a.get("OBJECTID") is not None else "unknown"
                )
                print(f"  warning: row build failed for {pid}: {e}")
                rows.append(normalize_row({
                    "parcel": pid, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("apache_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run (now at offset {offset})")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
