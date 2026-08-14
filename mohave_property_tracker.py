"""
Mohave County Property Tracker
-------------------------------
Builds a per-parcel property profile for Mohave County, Arizona from ONE
public bulk ArcGIS MapServer layer ("PARCELS/MapServer/14", named
"ParcelQueryLayer" -- clearly built for exactly this use case) published
by Mohave County IT (MCIT) on behalf of the Assessor's Office. Found via
the county's own open-data hub (az-mohave.opendata.arcgis.com) -> "ArcGIS
REST Services Directory" link -> mcgis.mohave.gov/arcgis/rest/services.
Free and unauthenticated, same pattern as every AZ county in this repo
except Yavapai (WAF-blocked, see COUNTIES.md).

~267,046 parcels as of 2026-08-13 (returnCountOnly confirmed) -- above the
~200k threshold where this repo switches to a resumable checkpoint
(same reasoning as maricopa_property_tracker.py / pinal_property_tracker.py):
progress is saved after every page in mohave_scrape_state (the ArcGIS
resultOffset to resume from), so a time-boxed run picks up where it left
off instead of always restarting from the top of the TAXPIN order. Once a
full sweep completes, the offset wraps back to 0 to keep the whole county
periodically re-synced.

This layer is unusually rich for valuation: it carries BOTH the raw
Full Cash Value / Limited Property Value (FULL_CASH_VALUE / LIMITED_VALUE,
i.e. the market/LPV figures before the state assessment ratio is applied)
AND the already-ratio-adjusted taxable amounts (ASSESSED_FULL_CASH_VALUE /
ASSESSED_LIMITED -- confirmed via a sample record: 3179 * 0.15 ratio =
476.85, which rounds to the sampled ASSESSED_FULL_CASH_VALUE of 477). Both
are kept since callers may want either the market value or the taxable
value. Real sale history (SALEP/SALEDT/DEEDTYPE/RECPTNO) and lat/lon are
also present, same as Yuma.

PARCEL_SIZE is a bare number with its unit given separately in UNIT_TYPE
(seen: "ACRES") -- same split-field pattern as Yuma's SIZE_/SIZE_UNIT, so
it's normalized to acres via size_to_acres() below rather than assumed.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55 (safety net -- a full
                               sweep may take several runs; each one just
                               continues from its checkpoint)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
import math

FEATURE_URL = "https://mcgis.mohave.gov/arcgis/rest/services/PARCELS/MapServer/14/query"

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to the county's server

OUT_FIELDS = (
    "TAXPIN,TAXPARCELTYPE,EXEMPTSTATUS,SITE_ADDRESS,OWNER,OWNER_2,"
    "MAILING_ADDRESS,CITY,STATE,ZIP,LEGAL_DESCRIPTION,TWN_RNG_SEC,"
    "USE_CODE,PROPTYPE,PROPUSE,PROPCODE,CLASS_CODE,PARCEL_SIZE,UNIT_TYPE,"
    "FULL_CASH_VALUE,LIMITED_VALUE,ASSESSED_FULL_CASH_VALUE,ASSESSED_LIMITED,"
    "ASSESSMENT_RATIO,LANDVALUE,IMPVALUE,"
    "SALEP,SALEDT,REC_BOOK,REC_PAGE,DEEDTYPE,RECPTNO,"
    "ACCOUNTNO,TAX_YEAR,TAX_AREA_CODE_FMT,BOS_DISTRICT,NBHD,"
    "LATITUDE,LONGITUDE,ALTITUDE"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredMohavePropertyTracker/1.0",
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


def epoch_ms_to_date(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def size_to_acres(size_value, unit_type):
    """PARCEL_SIZE is a bare number; UNIT_TYPE gives its unit separately
    (seen: "ACRES"). Normalize to acres rather than assume -- if an
    unrecognized unit shows up, keep the raw number rather than silently
    mis-converting it."""
    n = to_num(size_value)
    if n is None:
        return None
    unit = (clean(unit_type) or "").upper()
    if unit.startswith("ACR"):
        return n
    if unit.startswith("SQ") or unit.startswith("FT") or "FOOT" in unit or "FEET" in unit:
        return n / 43560.0
    return n


# ---------------------------------------------------------------
# Checkpoint (resumable offset, stored in Supabase -- 267k parcels /
# ~134 pages means a single run may not always finish a full sweep)
# ---------------------------------------------------------------
def get_offset() -> int:
    try:
        r = session.get(
            f"{SUPABASE_URL}/rest/v1/mohave_scrape_state"
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
    url = f"{SUPABASE_URL}/rest/v1/mohave_scrape_state?on_conflict=key"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    body = [{"key": "parcels_offset", "value": str(value),
             "updated_at": datetime.now(timezone.utc).isoformat()}]
    try:
        r = session.post(url, headers=headers, data=json.dumps(body), timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  warning: couldn't save checkpoint ({e})")


# ---------------------------------------------------------------
# Bulk ArcGIS MapServer pagination
# ---------------------------------------------------------------
def fetch_page(offset: int) -> list:
    params = {
        "where": "1=1",
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": "TAXPIN ASC",
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
    "jurisdiction", "parcel_type", "exempt_status",
    "property_address", "legal_description", "section_township_range",
    "owner_name", "owner_name_2",
    "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "use_code", "property_type", "property_use", "property_code", "class_code",
    "land_size_acres",
    "full_cash_value", "limited_value",
    "assessed_full_cash_value", "assessed_limited_value", "assessment_ratio",
    "land_value", "improvement_value",
    "sale_price", "sale_date", "deed_book", "deed_page", "deed_type", "receipt_number",
    "account_number", "tax_year", "tax_area_code", "bos_district", "neighborhood_code",
    "latitude", "longitude", "altitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "mohave_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("TAXPIN"))
    owner_name = clean(a.get("OWNER"))
    return {
        "parcel": parcel,
        "jurisdiction": "mohave_county",
        "parcel_type": clean(a.get("TAXPARCELTYPE")),
        "exempt_status": clean(a.get("EXEMPTSTATUS")),
        "property_address": clean(a.get("SITE_ADDRESS")),
        "legal_description": clean(a.get("LEGAL_DESCRIPTION")),
        "section_township_range": clean(a.get("TWN_RNG_SEC")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("OWNER_2")),
        "mailing_address": clean(a.get("MAILING_ADDRESS")),
        "mailing_city": clean(a.get("CITY")),
        "mailing_state": clean(a.get("STATE")),
        "mailing_zip": clean(a.get("ZIP")),
        "use_code": clean(a.get("USE_CODE")),
        "property_type": clean(a.get("PROPTYPE")),
        "property_use": clean(a.get("PROPUSE")),
        "property_code": clean(a.get("PROPCODE")),
        "class_code": clean(a.get("CLASS_CODE")),
        "land_size_acres": size_to_acres(a.get("PARCEL_SIZE"), a.get("UNIT_TYPE")),
        "full_cash_value": to_num(a.get("FULL_CASH_VALUE")),
        "limited_value": to_num(a.get("LIMITED_VALUE")),
        "assessed_full_cash_value": to_num(a.get("ASSESSED_FULL_CASH_VALUE")),
        "assessed_limited_value": to_num(a.get("ASSESSED_LIMITED")),
        "assessment_ratio": to_num(a.get("ASSESSMENT_RATIO")),
        "land_value": to_num(a.get("LANDVALUE")),
        "improvement_value": to_num(a.get("IMPVALUE")),
        "sale_price": to_num(a.get("SALEP")),
        "sale_date": epoch_ms_to_date(a.get("SALEDT")),
        "deed_book": clean(a.get("REC_BOOK")),
        "deed_page": clean(a.get("REC_PAGE")),
        "deed_type": clean(a.get("DEEDTYPE")),
        "receipt_number": clean(a.get("RECPTNO")),
        "account_number": clean(a.get("ACCOUNTNO")),
        "tax_year": clean(a.get("TAX_YEAR")),
        "tax_area_code": clean(a.get("TAX_AREA_CODE_FMT")),
        "bos_district": clean(a.get("BOS_DISTRICT")),
        "neighborhood_code": clean(a.get("NBHD")),
        "latitude": a.get("LATITUDE"),
        "longitude": a.get("LONGITUDE"),
        "altitude": a.get("ALTITUDE"),
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
                pin = a.get("TAXPIN") or "unknown"
                print(f"  warning: row build failed for {pin}: {e}")
                rows.append(normalize_row({
                    "parcel": pin, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        # Defensive de-dup, keeping the LAST row seen per parcel -- Postgres
        # rejects an upsert batch containing the same conflict key twice
        # ("ON CONFLICT DO UPDATE command cannot affect row a second time").
        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("mohave_property_info", deduped, on_conflict="parcel")

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
