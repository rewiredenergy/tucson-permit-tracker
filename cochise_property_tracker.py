"""
Cochise County Property Tracker
---------------------------------
Builds a per-parcel property profile for Cochise County, Arizona from ONE
public bulk ArcGIS FeatureServer layer ("Cad_Parcel_TaxInfo", titled
"Parcel (Cochise County - Tax Information)") published by Cochise County
GIS -- confirmed as the official source via the ArcGIS Online item search
API (owner CoconinoCountyGIS-equivalent org account "Cochise County GIS",
301k+ views, "updated weekly"). Free and unauthenticated, same pattern as
every AZ county in this repo except Yavapai (WAF-blocked, see COUNTIES.md).

~122,936 parcels as of 2026-08-13 (returnCountOnly confirmed) -- below the
~200k threshold, so this uses the simpler "re-pull the whole county every
run" design (no resumable checkpoint needed), same as Santa Cruz/Yuma.

Unlike Yuma/Mohave, this layer does NOT include sale price/date history --
just current owner/mailing info, valuation (FCV only, no separate LPV
field), acreage, and legal description. geo_x/geo_y are plain decimal-
degree lat/lon stored as STRINGS (not the layer's native projected
coordinate system) -- confirmed via a sample record ("-109.876032" /
"31.415556", matching Bisbee, AZ). address1 is frequently null with the
mailing street actually landing in address2 in sampled records -- both
are kept as separate columns rather than assumed to always be in one or
the other.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
import math

FEATURE_URL = "https://services6.arcgis.com/Yxem0VOcqSy8T6TE/arcgis/rest/services/Cad_Parcel_TaxInfo/FeatureServer/0/query"

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to Esri's hosted service

OUT_FIELDS = (
    "apn,reference,tax_year,accttype,accountno,tax_area_code,situs_address,"
    "owner_name1,owner_name2,address1,address2,city,state,zip_code,"
    "parcel_size,acres,use_code,legal_text,fcv,ag_operator,"
    "mkt_area,mkt_subarea,geo_x,geo_y"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredCochisePropertyTracker/1.0",
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


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination
# ---------------------------------------------------------------
def fetch_page(offset: int) -> list:
    params = {
        "where": "1=1",
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": "apn ASC",
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
            return [f["attributes"] for f in (d.get("features") or [])]
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"fetch failed at offset {offset}: {last_err}")


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------

# Every column a row might set, EXCLUDING "parcel" (always present, it's
# the primary key). Every upserted row -- normal or a defensive error row
# -- gets padded to this exact column set: PostgREST's bulk upsert rejects
# a batch whose objects don't all have EXACTLY the same keys (PGRST102
# "All object keys must match").
ROW_COLUMNS = [
    "jurisdiction", "reference", "tax_year", "account_type", "account_number",
    "tax_area_code", "property_address",
    "owner_name", "owner_name_2",
    "mailing_address_1", "mailing_address_2", "mailing_city", "mailing_state", "mailing_zip",
    "land_size_acres", "land_size_raw", "use_code", "legal_description",
    "full_cash_value", "ag_operator", "market_area", "market_subarea",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "cochise_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("apn"))
    owner_name = clean(a.get("owner_name1"))
    return {
        "parcel": parcel,
        "jurisdiction": "cochise_county",
        "reference": clean(a.get("reference")),
        "tax_year": clean(a.get("tax_year")),
        "account_type": clean(a.get("accttype")),
        "account_number": clean(a.get("accountno")),
        "tax_area_code": clean(a.get("tax_area_code")),
        "property_address": clean(a.get("situs_address")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("owner_name2")),
        "mailing_address_1": clean(a.get("address1")),
        "mailing_address_2": clean(a.get("address2")),
        "mailing_city": clean(a.get("city")),
        "mailing_state": clean(a.get("state")),
        "mailing_zip": clean(a.get("zip_code")),
        "land_size_acres": to_num(a.get("acres")),
        "land_size_raw": to_num(a.get("parcel_size")),
        "use_code": clean(a.get("use_code")),
        "legal_description": clean(a.get("legal_text")),
        "full_cash_value": to_num(a.get("fcv")),
        "ag_operator": clean(a.get("ag_operator")),
        "market_area": clean(a.get("mkt_area")),
        "market_subarea": clean(a.get("mkt_subarea")),
        "latitude": to_num(a.get("geo_y")),
        "longitude": to_num(a.get("geo_x")),
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
    print(f"Run started {now_iso}"
          + (f" -- TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else "")
          + f" -- runtime budget {MAX_RUNTIME_MINUTES} min")

    processed = 0
    pages = 0
    offset = 0

    while True:
        elapsed_min = (time.monotonic() - _start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"Reached runtime budget ({MAX_RUNTIME_MINUTES} min) -- "
                  f"stopping early ({processed} parcels this run).")
            break
        if SAMPLE_LIMIT and processed >= SAMPLE_LIMIT:
            print(f"Reached sample limit ({SAMPLE_LIMIT}) -- stopping.")
            break

        time.sleep(REQUEST_DELAY)
        raw_rows = fetch_page(offset)
        if not raw_rows:
            print(f"Reached the end of the parcel list at offset {offset} -- full sweep complete.")
            break

        rows = []
        for a in raw_rows:
            try:
                rows.append(normalize_row(build_row(a, now_iso)))
            except Exception as e:  # noqa: BLE001
                apn = a.get("apn") or "unknown"
                print(f"  warning: row build failed for {apn}: {e}")
                rows.append(normalize_row({
                    "parcel": apn, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        # Defensive de-dup, keeping the LAST row seen per parcel -- Postgres
        # rejects an upsert batch containing the same conflict key twice
        # ("ON CONFLICT DO UPDATE command cannot affect row a second time").
        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("cochise_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
