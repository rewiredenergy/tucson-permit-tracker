"""
Navajo County Property Tracker
---------------------------------
Builds a per-parcel property profile for Navajo County, Arizona from ONE
public bulk ArcGIS FeatureServer layer ("Parcels") published by Navajo
County -- confirmed as the official source via the ArcGIS Online item
search API (owner account "NavajoCounty"), same pattern as every AZ county
in this repo except Yavapai (WAF-blocked, see COUNTIES.md) and Coconino
(deferred, no bulk owner data -- see COUNTIES.md).

~86,849 parcels as of 2026-08-13 (returnCountOnly confirmed) -- below the
~200k threshold, so this uses the simpler "re-pull the whole county every
run" design (no resumable checkpoint needed), same as Santa Cruz/Yuma/
Cochise.

IMPORTANT GAP: unlike most other counties in this repo, this layer has NO
valuation fields at all (no full cash value, assessed value, land/
improvement value) and no sale price/date history. What IS present in
bulk: owner name, mailing address, situs (property) address, legal
description, subdivision, building count, account type, status,
jurisdiction, zoning, acreage, and lat/lon. The county's fuller per-parcel
detail (valuation, tax history) lives behind
apps.navajocountyaz.gov/navajowebpayments/propertyinformation -- a
per-parcel web app (URL template captured per-row as `parcel_detail_url`
for reference/follow-up), not a bulk API. This is still meaningfully
better than Coconino (which lacks owner data entirely in bulk), so it was
built rather than deferred -- just be aware valuation is absent here.

MailingAddress is a single field with embedded \\r\\n line breaks (street
line(s) then "City, ST ZIPZIP" with no separator between state and zip in
samples seen) -- not parsed into separate city/state/zip columns because
the format wasn't consistent/reliable enough across samples; kept as a
single raw column alongside the cleaner MailingAddressLine1/Line2 fields
the source also provides.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FEATURE_URL = "https://services.arcgis.com/cghC2lEIpJ2TRrs5/arcgis/rest/services/Parcels/FeatureServer/0/query"

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to Esri's hosted service

OUT_FIELDS = (
    "AccountNumber,APN,Owner,Sheet,TRS,Legal,Longitude,Latitude,"
    "SitusAddress,MailingAddress,MailingAddressLine1,MailingAddressLine2,"
    "Subdivision,BuildingCount,AccountType,Status,Jurisdiction,Zoning,"
    "Acreage,ParcelDetailPath"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredNavajoPropertyTracker/1.0",
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
    "jurisdiction", "account_number", "sheet", "trs", "legal_description",
    "property_address", "owner_name",
    "mailing_address_1", "mailing_address_2", "mailing_address_raw",
    "subdivision", "building_count", "account_type", "parcel_status",
    "zoning", "land_size_acres", "parcel_detail_url",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "navajo_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("APN"))
    owner_name = clean(a.get("Owner"))
    return {
        "parcel": parcel,
        "jurisdiction": "navajo_county",
        "account_number": clean(a.get("AccountNumber")),
        "sheet": clean(a.get("Sheet")),
        "trs": clean(a.get("TRS")),
        "legal_description": clean(a.get("Legal")),
        "property_address": clean(a.get("SitusAddress")),
        "owner_name": owner_name,
        "mailing_address_1": clean(a.get("MailingAddressLine1")),
        "mailing_address_2": clean(a.get("MailingAddressLine2")),
        "mailing_address_raw": clean(a.get("MailingAddress")),
        "subdivision": clean(a.get("Subdivision")),
        "building_count": to_num(a.get("BuildingCount")),
        "account_type": clean(a.get("AccountType")),
        "parcel_status": clean(a.get("Status")),
        "zoning": clean(a.get("Zoning")),
        "land_size_acres": to_num(a.get("Acreage")),
        "parcel_detail_url": clean(a.get("ParcelDetailPath")),
        "latitude": to_num(a.get("Latitude")),
        "longitude": to_num(a.get("Longitude")),
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
                apn = a.get("APN") or "unknown"
                print(f"  warning: row build failed for {apn}: {e}")
                rows.append(normalize_row({
                    "parcel": apn, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        # Defensive de-dup, keeping the LAST row seen per parcel -- Postgres
        # rejects an upsert batch containing the same conflict key twice
        # ("ON CONFLICT DO UPDATE command cannot affect row a second time").
        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("navajo_property_info", deduped, on_conflict="parcel")

        processed += len(deduped)
        pages += 1
        offset += PAGE_SIZE
        if pages % 10 == 0:
            print(f"  {pages} pages / {processed} parcels this run")

    print(f"Done. Upserted {processed} parcels this run across {pages} pages.")


if __name__ == "__main__":
    main()
