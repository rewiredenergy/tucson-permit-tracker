"""
Pinal County Property Tracker
----------------------------------
Builds a per-parcel property profile for Pinal County, Arizona (~287k
parcels -- larger than Santa Cruz's ~43k, smaller than Maricopa's
~1.76M) from ONE public bulk ArcGIS FeatureServer layer ("TaxParcels")
published directly by Pinal County's own GIS server. This is the exact
same backend that powers the county's public Assessor Parcel Viewer
(pinal.maps.arcgis.com) -- confirmed free and unauthenticated, no API
token needed. A third-party mirror of a similarly-named layer exists on
Casa Grande's city GIS server (rogue.casagrandeaz.gov) but only carries
~60k parcels (a partial/regional subset) -- gis.pinal.gov's own
TaxParcels/FeatureServer/3 layer is the authoritative full-county
source (confirmed via returnCountOnly: 286,958 parcels).

Like Santa Cruz and Maricopa, owner name, mailing address, year built
(RESYRBLT), living area (RESFLRAREA), and both current and prior year
assessed/taxable values are all present on this one layer already --
no separate per-parcel enrichment step needed. This is a bulk paginated
pull (2000 rows/page, the layer's maxRecordCount), not a rate-limited
per-parcel API loop.

At ~287k parcels this comfortably exceeds the >200k-parcel threshold
where a single scheduled run isn't guaranteed to finish a full sweep,
so -- exactly like maricopa_property_tracker.py -- progress is
checkpointed after every page in the pinal_scrape_state table (the
ArcGIS resultOffset to resume from). Once a full sweep completes, the
offset wraps back to 0 so the whole county keeps getting periodically
re-synced (values, sales, and owners change over time).

One field quirk worth flagging (see COUNTIES.md step 1 on always
sample-checking real field formats before writing parsing code):
LGLSTARTDT and LASTUPDATE are epoch-millisecond esriFieldTypeDate
fields, but SALEDATE is a plain "YYYY-MM-DD" *string* on this layer --
NOT epoch milliseconds like Maricopa's SALE_DATE. Confirmed against
live sample rows before writing parse_sale_date() below.

The layer's own description notes it "contains redundant geometry in
cases where there are multiple condominium units on a given tax
parcel" -- i.e. a PARCELID can repeat across a few rows for split
condo units. Handled the same defensive way as every other tracker
here: de-dup a batch by parcel right before upsert, keeping the last
row seen (Postgres otherwise rejects the batch with 21000 "ON CONFLICT
DO UPDATE command cannot affect row a second time").

COORDINATES: this layer's attribute table has no LATITUDE/LONGITUDE (or
X/Y) fields at all -- confirmed via the layer's field list. Rather than
pull full polygon geometry (expensive at this row count), the request
below asks ArcGIS to compute a per-feature centroid server-side with
returnCentroid=true&outSR=4326 (combined with returnGeometry=false so
the full polygon is never transferred). Verified live against this
exact layer: a two-row test query returned
{"centroid": {"x": -111.197..., "y": 32.505...}} per feature, i.e.
x=longitude, y=latitude in WGS84 decimal degrees. fetch_page() below
merges that centroid into each row's attributes dict under synthetic
keys (_LATITUDE/_LONGITUDE, underscore-prefixed so they can't collide
with a real ArcGIS field name) before handing rows to build_row().

REGRESSION NOTE (2026-08-19): the COORDINATES block above was first
added in 7c395ec (8/13) and then silently undone hours later by the
same-day refactor 27a9405, which restored fetch_page()/build_row() to
their pre-fix shape. That regression is why ~99.3% of
pinal_property_info rows carried NULL coordinates while still
reporting status="enriched". Re-applied here -- do not drop these
params or the latitude/longitude row keys without also updating the
schema and the backfill expectations downstream in Knockzy.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55 (safety net -- a full
                               sweep of the whole county may take a few
                               runs; each run just continues from its
                               checkpoint)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import math

FEATURE_URL = ("https://gis.pinal.gov/mapping/rest/services/TaxParcels/"
               "FeatureServer/3/query")

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the county's server

OUT_FIELDS = (
    "PARCELID,OWNERNME1,OWNERNME2,SITEADDRESS,PSTLADDRESS,PSTLCITY,PSTLSTATE,"
    "PSTLZIP5,PSTLZIP4,PRPRTYDSCRP,CNVYNAME,CLASSDSCRP,USEDSCRP,"
    "RESYRBLT,RESFLRAREA,RESSTRTYP,FLOORCOUNT,"
    "LNDVALUE,PRVASSDVAL,CNTASSDVAL,PRVTXBLVAL,CNTTXBLVAL,"
    "SALEPRICE,SALEDATE,GROSSAC,LANDSF,LASTUPDATE"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredPinalPropertyTracker/1.0",
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

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


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


def to_int(value):
    n = to_num(value)
    return int(n) if n is not None else None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


def epoch_ms_to_date(value):
    """For LGLSTARTDT / LASTUPDATE -- true esriFieldTypeDate fields on
    this layer, stored as epoch milliseconds."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def parse_sale_date(value):
    """SALEDATE on this layer is a plain 'YYYY-MM-DD' STRING, not epoch
    milliseconds like Maricopa's SALE_DATE -- confirmed against live
    sample rows (e.g. "2013-06-01") before writing this. Just validates
    the shape and trims any trailing time component rather than
    re-parsing as a timestamp."""
    s = clean(value)
    if not s:
        return None
    m = _DATE_RE.match(s)
    return m.group(0) if m else None


# ---------------------------------------------------------------
# Checkpoint (resumable offset, stored in Supabase -- see module
# docstring for why this matters at ~287k rows / ~144 pages)
# ---------------------------------------------------------------
def get_offset() -> int:
    try:
        r = session.get(
            f"{SUPABASE_URL}/rest/v1/pinal_scrape_state"
            f"?key=eq.parcels_offset&select=value&limit=1",
            headers=SUPABASE_HEADERS, timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        return int(rows[0]["value"]) if rows else 0
    except Exception as e:  # noqa: BLE001
        print(f"  couldn't read checkpoint ({e}) -- starting from offset 0")
        return 0


def set_offset(value: int) -> None:
    url = f"{SUPABASE_URL}/rest/v1/pinal_scrape_state?on_conflict=key"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    body = [{"key": "parcels_offset", "value": str(value),
             "updated_at": datetime.now(timezone.utc).isoformat()}]
    try:
        r = session.post(url, headers=headers, data=json.dumps(body), timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  warning: couldn't save checkpoint ({e})")


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination
# ---------------------------------------------------------------
def fetch_page(offset: int) -> list:
    params = {
        "where": "1=1",
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "returnCentroid": "true",  # server-computed lat/lon without pulling full polygon geometry
        "outSR": "4326",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": "PARCELID ASC",
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

# Every column a row might set, EXCLUDING "parcel" (always present,
# it's the primary key). Every upserted row -- normal or a defensive
# error row -- gets padded to this exact column set: PostgREST's bulk
# upsert rejects a batch whose objects don't all have EXACTLY the same
# keys (PGRST102 "All object keys must match"), the same crash Pima's
# property_info_tracker.py hit in production before that fix was added
# there. jurisdiction defaults to "pinal_county" since the column is
# NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "property_description", "subdivision",
    "owner_name", "owner_name_2",
    "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "property_class", "property_use",
    "year_built", "living_area_sqft", "structure_type", "floor_count",
    "land_value", "assessed_value_current", "assessed_value_previous",
    "taxable_value_current", "taxable_value_previous",
    "sale_price", "sale_date",
    "land_size_acres", "land_size_sqft",
    "source_last_updated",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "pinal_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("PARCELID"))
    owner_name = clean(a.get("OWNERNME1"))
    zip5 = clean(a.get("PSTLZIP5"))
    zip4 = clean(a.get("PSTLZIP4"))
    mailing_zip = f"{zip5}-{zip4}" if zip5 and zip4 else zip5
    return {
        "parcel": parcel,
        "jurisdiction": "pinal_county",
        "property_address": clean(a.get("SITEADDRESS")),
        "property_description": clean(a.get("PRPRTYDSCRP")),
        "subdivision": clean(a.get("CNVYNAME")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("OWNERNME2")),
        "mailing_address": clean(a.get("PSTLADDRESS")),
        "mailing_city": clean(a.get("PSTLCITY")),
        "mailing_state": clean(a.get("PSTLSTATE")),
        "mailing_zip": mailing_zip,
        # e.g. "Owner Occupied Residential" / "Non-Primary Residence" /
        # "Vacant Land / Non-Profit Imp" / "Residential Common Areas" --
        # the most useful single field for a residential/non-residential
        # split downstream in Knockzy.
        "property_class": clean(a.get("CLASSDSCRP")),
        "property_use": clean(a.get("USEDSCRP")),
        "year_built": to_int(a.get("RESYRBLT")),
        "living_area_sqft": to_num(a.get("RESFLRAREA")),
        "structure_type": clean(a.get("RESSTRTYP")),
        "floor_count": to_int(a.get("FLOORCOUNT")),
        "land_value": to_num(a.get("LNDVALUE")),
        "assessed_value_current": to_num(a.get("CNTASSDVAL")),
        "assessed_value_previous": to_num(a.get("PRVASSDVAL")),
        "taxable_value_current": to_num(a.get("CNTTXBLVAL")),
        "taxable_value_previous": to_num(a.get("PRVTXBLVAL")),
        "sale_price": to_num(a.get("SALEPRICE")),
        "sale_date": parse_sale_date(a.get("SALEDATE")),
        "land_size_acres": to_num(a.get("GROSSAC")),
        "land_size_sqft": to_num(a.get("LANDSF")),
        "source_last_updated": epoch_ms_to_date(a.get("LASTUPDATE")),
        # Synthetic keys merged in by fetch_page() from the server-computed
        # centroid (x=lon, y=lat, WGS84) -- see the COORDINATES note in the
        # module docstring. to_num() rejects the non-finite values ArcGIS
        # returns for degenerate/zero-area parcels.
        "latitude": to_num(a.get("_LATITUDE")),
        "longitude": to_num(a.get("_LONGITUDE")),
        # No owner on file (right-of-way, common area, etc.) is a normal,
        # expected outcome here -- not a scrape failure.
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
    offset = get_offset()
    print(f"Run started {now_iso}"
          + (f" -- TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else "")
          + f" -- runtime budget {MAX_RUNTIME_MINUTES} min -- resuming at offset {offset}")

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
            print(f"Reached the end of the parcel list at offset {offset} -- "
                  f"full sweep complete! Wrapping back to offset 0 for the next run.")
            offset = 0
            set_offset(offset)
            break

        rows = []
        for a in raw_rows:
            try:
                rows.append(normalize_row(build_row(a, now_iso)))
            except Exception as e:  # noqa: BLE001
                pid = a.get("PARCELID") or "unknown"
                print(f"  warning: row build failed for {pid}: {e}")
                rows.append(normalize_row({
                    "parcel": pid, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        # Defensive de-dup, keeping the LAST row seen per parcel. This
        # layer can carry a few rows sharing the same PARCELID for
        # split condo units (see module docstring); Postgres rejects an
        # upsert batch containing the same conflict key twice ("ON
        # CONFLICT DO UPDATE command cannot affect row a second time").
        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("pinal_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        set_offset(offset)  # checkpoint after every successful page
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run "
                  f"(now at offset {offset})")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
