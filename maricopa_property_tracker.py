"""
Maricopa County Property Tracker
----------------------------------
Builds a per-parcel property profile for Maricopa County, Arizona --
by far the largest county in this repo (~1.76M parcels, vs ~448k for
Pima and ~43k for Santa Cruz) -- entirely from ONE public bulk ArcGIS
FeatureServer layer ("Parcels_view") published by the Assessor's Office.
This is the exact same backend that powers the Assessor's own public
Parcel Viewer at maps.mcassessor.maricopa.gov -- no paid Data Sales
download (those run $65-$1,500, see mcassessor.maricopa.gov/page/data_sales/)
and no API token needed (the documented mcassessor.maricopa.gov REST API
requires a free token via their Contact Us form; this tracker avoids
that entirely by using the same public map layer the county's own
website already serves to anonymous visitors).

Unlike Pima's property_info_tracker.py, there's no separate per-parcel
enrichment step -- owner name, mailing address, year built
(CONST_YEAR), living space (LIVING_SPACE), and both current and prior
year assessed values are all present on this one layer already. Like
Santa Cruz's tracker, this is a bulk paginated pull (2000 rows/page),
not a rate-limited per-parcel API loop.

Where this DOES differ from Santa Cruz: 1.76M parcels is roughly 40x
Santa Cruz's county, so a single run may not always finish a full
sweep. Progress is checkpointed after every page in the
maricopa_scrape_state table (the ArcGIS resultOffset to resume from),
so a time-boxed or interrupted run picks up where it left off next
time instead of always restarting from the top of the alphabet --
which would otherwise starve parcels late in APN order of ever being
refreshed. Once a full sweep completes, the offset wraps back to 0 so
the whole county keeps getting periodically re-synced (values, sales,
and owners change over time).

As with the other trackers, every upsert batch is normalized to an
identical column set and de-duplicated by parcel before being sent to
Supabase -- lessons learned the hard way from Pima's
property_info_tracker.py (PGRST102 "All object keys must match" and
Postgres 21000 "ON CONFLICT DO UPDATE command cannot affect row a
second time").

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55 (safety net -- a full
                               sweep of the whole county may take several
                               runs; each run just continues from its
                               checkpoint)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FEATURE_URL = ("https://services.arcgis.com/ykpntM6e3tHvzKRJ/arcgis/rest/"
               "services/Parcels_view/FeatureServer/0/query")

PAGE_SIZE = 2000
REQUEST_DELAY = 0.3  # seconds between page requests -- be polite to Esri's hosted service

OUT_FIELDS = (
    "APN_DASH,OWNER_NAME,PHYSICAL_ADDRESS,MAIL_ADDRESS,INCAREOF,"
    "JURISDICTION,SUBNAME,LOT_NUM,STR,"
    "FCV_CUR,LPV_CUR,LC_CUR,TAX_YR_CUR,FCV_PREV,LPV_PREV,LC_PREV,TAX_YR_PREV,"
    "SALE_PRICE,SALE_DATE,DEED_NUMBER,DEED_DATE,"
    "CONST_YEAR,LIVING_SPACE,LAND_SIZE,FLOOR,PUC,CITY_ZONING,"
    "LATITUDE,LONGITUDE"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredMaricopaPropertyTracker/1.0",
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
    """Handles both plain numeric fields and the padded/comma'd string
    fields this layer returns for money/sqft columns (e.g. "     537,200"
    or "   2,266")."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(value):
    n = to_num(value)
    return int(n) if n is not None else None


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


# ---------------------------------------------------------------
# Checkpoint (resumable offset, stored in Supabase -- see module
# docstring for why this matters at 1.76M rows / ~880 pages)
# ---------------------------------------------------------------
def get_offset() -> int:
    try:
        r = session.get(
            f"{SUPABASE_URL}/rest/v1/maricopa_scrape_state"
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
    url = f"{SUPABASE_URL}/rest/v1/maricopa_scrape_state?on_conflict=key"
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
# the primary key). Every upserted row -- normal or a defensive error
# row -- gets padded to this exact column set: PostgREST's bulk upsert
# rejects a batch whose objects don't all have EXACTLY the same keys
# (PGRST102 "All object keys must match"), the same crash Pima's
# property_info_tracker.py hit in production before this fix was added
# there. jurisdiction defaults to "maricopa_county" rather than None
# since the column is NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "municipality", "property_address", "owner_name",
    "mailing_address", "care_of", "subdivision", "lot_number",
    "section_township_range",
    "full_cash_value_current", "limited_value_current", "legal_class_current",
    "tax_year_current", "full_cash_value_previous", "limited_value_previous",
    "legal_class_previous", "tax_year_previous",
    "sale_price", "sale_date", "deed_number", "deed_date",
    "construction_year", "living_space_sqft", "land_size_sqft",
    "floor_count", "property_use_code", "zoning",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "maricopa_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("APN_DASH"))
    owner_name = clean(a.get("OWNER_NAME"))
    return {
        "parcel": parcel,
        "jurisdiction": "maricopa_county",
        "municipality": clean(a.get("JURISDICTION")),
        "property_address": clean(a.get("PHYSICAL_ADDRESS")),
        "owner_name": owner_name,
        "mailing_address": clean(a.get("MAIL_ADDRESS")),
        "care_of": clean(a.get("INCAREOF")),
        "subdivision": clean(a.get("SUBNAME")),
        "lot_number": clean(a.get("LOT_NUM")),
        "section_township_range": clean(a.get("STR")),
        "full_cash_value_current": to_num(a.get("FCV_CUR")),
        "limited_value_current": to_num(a.get("LPV_CUR")),
        "legal_class_current": clean(a.get("LC_CUR")),
        "tax_year_current": clean(a.get("TAX_YR_CUR")),
        "full_cash_value_previous": to_num(a.get("FCV_PREV")),
        "limited_value_previous": to_num(a.get("LPV_PREV")),
        "legal_class_previous": clean(a.get("LC_PREV")),
        "tax_year_previous": clean(a.get("TAX_YR_PREV")),
        "sale_price": to_num(a.get("SALE_PRICE")),
        "sale_date": epoch_ms_to_date(a.get("SALE_DATE")),
        "deed_number": clean(a.get("DEED_NUMBER")),
        "deed_date": epoch_ms_to_date(a.get("DEED_DATE")),
        "construction_year": to_int(a.get("CONST_YEAR")),
        "living_space_sqft": to_num(a.get("LIVING_SPACE")),
        "land_size_sqft": to_num(a.get("LAND_SIZE")),
        "floor_count": to_num(a.get("FLOOR")),
        "property_use_code": clean(a.get("PUC")),
        "zoning": clean(a.get("CITY_ZONING")),
        "latitude": a.get("LATITUDE"),
        "longitude": a.get("LONGITUDE"),
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
                apn = a.get("APN_DASH") or "unknown"
                print(f"  warning: row build failed for {apn}: {e}")
                rows.append(normalize_row({
                    "parcel": apn, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                }))

        # Defensive de-dup, keeping the LAST row seen per parcel. Each page
        # is already unique by APN, but Postgres rejects an upsert batch
        # containing the same conflict key twice ("ON CONFLICT DO UPDATE
        # command cannot affect row a second time") -- the same failure
        # Pima's tracker hit in production. Cheap insurance.
        deduped = list({r["parcel"]: r for r in rows}.values())
        upsert("maricopa_property_info", deduped, on_conflict="parcel")

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
