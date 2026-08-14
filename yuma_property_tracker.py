"""
Yuma County Property Tracker
-----------------------------
Builds a per-parcel property profile for Yuma County, Arizona (~70k
parcels), pulled from a single public bulk ArcGIS MapServer layer
("County Parcels", layer 0 of the "pan/Parcels" service) hosted on the
City of Yuma's GIS server (gis.ci.yuma.az.us). The service description
states it's an automated weekly export of Yuma County Tax Assessor
records merged with the City's parcel feature class -- so despite living
on a City of Yuma host, this is the county assessor data. No API token
needed.

Unlike Yavapai, this layer carries real valuation and sale data:
LAND_FCV / IMPROVEMEN (improvement value) / TOTAL_FCV (full cash value) /
TOTAL_LPV (limited property value, used for AZ tax calc), plus
SALEDOCNUM / SALE_DATE / SALE_PRICE, and even LATITUDE/LONGITUDE. This is
one of the richer sources in this repo.

SIZE_ is a bare number whose unit is given separately in SIZE_UNIT ("A"
for acres, "F" for square feet, seen in samples) -- normalized to
land_size_acres below (F values divided by 43,560). SQFT_GISCALC is the
GIS-calculated polygon area of the parcel itself (same idea as
Shape.STArea()), NOT a building/structure square footage -- there's no
building sqft field in this layer, so it's stored as parcel_sqft_gis for
reference, not confused with living-area sqft.

At ~70k parcels (under the ~200k threshold used elsewhere in this repo),
a full sweep completes in a few minutes -- no resumable checkpoint table
needed, same simpler design as Santa Cruz's tracker. The run is still
time-boxed via MAX_RUNTIME_MINUTES as a safety net.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL              e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT               optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES        optional; default 55
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
import math

FEATURE_URL = "https://gis.ci.yuma.az.us/server/rest/services/pan/Parcels/MapServer/0/query"

PAGE_SIZE = 2000

OUT_FIELDS = (
    "PARCEL_ID,PARSED_ID,ACCOUNTNO,TAXYEAR,PRIM_OWNER,SEC_OWNER,BUSINESS,"
    "OWNER_ADDR,OWNER_CITY,OWNER_STAT,OWNER_ZIP,SITUS_ADDR,LEGAL_SUMM,"
    "SIZE_,SIZE_UNIT,SQFT_GISCALC,LAND_FCV,IMPROVEMEN,TOTAL_FCV,TOTAL_LPV,"
    "SALEDOCNUM,SALE_DATE,SALE_PRICE,SUBDIVISIO,BLOCK,LOT,PROPERTYCODE,"
    "MOBILEHOMESPACE,LATITUDE,LONGITUDE"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
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
    try:
        v = float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None
    # ArcGIS occasionally returns the literal string "NaN" (or "Infinity")
    # for a degenerate/zero-area parcel's computed centroid -- float() parses
    # those "successfully" into a non-finite value, which then breaks
    # PostgREST's strict JSON parser downstream ("Empty or invalid json"),
    # crashing the whole batch upsert. Reject non-finite results here instead.
    return v if (v is None or math.isfinite(v)) else None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


def epoch_ms_to_date(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def size_to_acres(size_value, size_unit):
    n = to_num(size_value)
    if n is None:
        return None
    unit = (clean(size_unit) or "").upper()
    if unit.startswith("F"):
        return n / 43560.0
    return n


# ---------------------------------------------------------------
# Bulk ArcGIS MapServer pagination -- single layer, no join needed
# ---------------------------------------------------------------
def fetch_all_parcels() -> list:
    features = []
    offset = 0
    while True:
        elapsed_min = (time.monotonic() - _start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"  Reached runtime budget ({MAX_RUNTIME_MINUTES} min) mid-fetch -- "
                  f"stopping with {len(features)} rows so far.")
            break
        params = {
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": "PARCEL_ID ASC",
            "f": "json",
        }
        last_err = None
        d = None
        for attempt in range(1, 4):
            try:
                r = session.get(FEATURE_URL, params=params, timeout=60)
                r.raise_for_status()
                d = r.json()
                if "error" in d:
                    raise RuntimeError(str(d["error"])[:200])
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(3 * attempt)
        if last_err is not None:
            raise RuntimeError(f"Parcels fetch failed at offset {offset}: {last_err}")
        feats = d.get("features") or []
        if not feats:
            break
        features.extend(f["attributes"] for f in feats)
        if len(features) % 20000 < PAGE_SIZE:
            print(f"  fetched {len(features)} rows so far")
        offset += PAGE_SIZE
        if SAMPLE_LIMIT and len(features) >= SAMPLE_LIMIT:
            break
        if len(feats) < PAGE_SIZE:
            break
    return features


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------

# Every column a row might set, EXCLUDING "parcel" (always present, it's
# the primary key). Every upserted row -- normal or a defensive error
# row -- gets padded to this exact column set: PostgREST's bulk upsert
# rejects a batch whose objects don't all have EXACTLY the same keys
# (PGRST102 "All object keys must match").
ROW_COLUMNS = [
    "jurisdiction", "parcel_label", "property_address", "legal_description",
    "subdivision", "block", "lot",
    "owner_name", "owner_name_2", "business_name",
    "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "land_size_acres", "parcel_sqft_gis",
    "land_value", "improvement_value", "total_value", "limited_value",
    "sale_document", "sale_date", "sale_price",
    "account_number", "tax_year", "property_code", "mobile_home_space",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "yuma_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("PARCEL_ID"))
    owner_name = clean(a.get("PRIM_OWNER"))
    return {
        "parcel": parcel,
        "jurisdiction": "yuma_county",
        "parcel_label": clean(a.get("PARSED_ID")),
        "property_address": clean(a.get("SITUS_ADDR")),
        "legal_description": clean(a.get("LEGAL_SUMM")),
        "subdivision": clean(a.get("SUBDIVISIO")),
        "block": clean(a.get("BLOCK")),
        "lot": clean(a.get("LOT")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("SEC_OWNER")),
        "business_name": clean(a.get("BUSINESS")),
        "mailing_address": clean(a.get("OWNER_ADDR")),
        "mailing_city": clean(a.get("OWNER_CITY")),
        "mailing_state": clean(a.get("OWNER_STAT")),
        "mailing_zip": clean(a.get("OWNER_ZIP")),
        "land_size_acres": size_to_acres(a.get("SIZE_"), a.get("SIZE_UNIT")),
        "parcel_sqft_gis": to_num(a.get("SQFT_GISCALC")),
        "land_value": to_num(a.get("LAND_FCV")),
        "improvement_value": to_num(a.get("IMPROVEMEN")),
        "total_value": to_num(a.get("TOTAL_FCV")),
        "limited_value": to_num(a.get("TOTAL_LPV")),
        "sale_document": clean(a.get("SALEDOCNUM")),
        "sale_date": epoch_ms_to_date(a.get("SALE_DATE")),
        "sale_price": to_num(a.get("SALE_PRICE")),
        "account_number": clean(a.get("ACCOUNTNO")),
        "tax_year": clean(a.get("TAXYEAR")),
        "property_code": clean(a.get("PROPERTYCODE")),
        "mobile_home_space": clean(a.get("MOBILEHOMESPACE")),
        "latitude": to_num(a.get("LATITUDE")),
        "longitude": to_num(a.get("LONGITUDE")),
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
    total = len(rows)
    done = 0
    for i in range(0, total, 500):
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
        done += len(batch)
        print(f"  saved batch of {len(batch)} ({done}/{total})")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"Run started {now_iso}"
          + (f" -- TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else "")
          + f" -- runtime budget {MAX_RUNTIME_MINUTES} min")

    raw_rows = fetch_all_parcels()
    if SAMPLE_LIMIT:
        raw_rows = raw_rows[:SAMPLE_LIMIT]

    rows = []
    for a in raw_rows:
        try:
            rows.append(normalize_row(build_row(a, now_iso)))
        except Exception as e:  # noqa: BLE001
            pid = a.get("PARCEL_ID") or "unknown"
            print(f"  warning: row build failed for {pid}: {e}")
            rows.append(normalize_row({
                "parcel": pid, "status": "error",
                "error_note": str(e)[:300], "updated_at": now_iso,
            }))

    # Defensive de-dup, keeping the LAST row seen per parcel -- Postgres
    # rejects an upsert batch containing the same conflict key twice
    # ("ON CONFLICT DO UPDATE command cannot affect row a second time").
    deduped = list({r["parcel"]: r for r in rows}.values())

    print(f"Upserting {len(deduped)} parcels to Supabase...")
    upsert("yuma_property_info", deduped, on_conflict="parcel")
    print(f"Done. Upserted {len(deduped)} parcels this run.")


if __name__ == "__main__":
    main()
