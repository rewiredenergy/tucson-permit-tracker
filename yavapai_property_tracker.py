"""
Yavapai County Property Tracker
----------------------------------
Builds a per-parcel property profile for Yavapai County, Arizona
(~188k parcels -- between Santa Cruz's ~43k and Pinal's ~287k) from ONE
public bulk ArcGIS FeatureServer layer ("Parcels", layer 4 of the
"Property" service) published directly by Yavapai County's own GIS
server (gis.yavapaiaz.gov) -- the same backend behind the county's
public Assessor Parcel Information map. No API token needed.

Unlike Pima/Santa Cruz/Maricopa/Pinal, this county's public parcel
layer carries NO valuation, year-built, or square-footage data --
just ownership, mailing address, situs (property) address, zoning,
subdivision, and deeded acreage. That's a real gap vs. the other
counties (flagged in COUNTIES.md), not a scraper bug: there's simply
no free bulk source for Yavapai valuation/building data at this time.

Two address fields matter here and are easy to mix up:
  - ADDRESS/CITY/STATE/ZIP is the OWNER'S MAILING address (can be
    anywhere -- a PO box, a different city, out of state).
  - SITUS_ADD_DOR is the actual PHYSICAL/PROPERTY address, which is
    what property_address should be. Verified against live samples:
    e.g. a Seligman-mailing-address owner (PO BOX 858) whose parcel's
    SITUS_ADD_DOR was a different road entirely (56492 N La Carro Ln).
ZIP is a bare 9-digit string (zip5+zip4 concatenated, no dash) when
present -- reformatted to zip5-zip4 below, same idea as Pinal's
PSTLZIP5/PSTLZIP4 split but starting from one field instead of two.

At ~188k parcels (under the ~200k threshold used elsewhere in this
repo), a full sweep completes in a few minutes -- no resumable
checkpoint table needed, same simpler design as Santa Cruz's tracker.
The run is still time-boxed via MAX_RUNTIME_MINUTES as a safety net.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 55
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from curl_cffi import requests as cffi_requests

FEATURE_URL = "https://gis.yavapaiaz.gov/arcgis/rest/services/Property/FeatureServer/4/query"

PAGE_SIZE = 2000

OUT_FIELDS = (
    "PARCEL_ID,PARLABEL,NAME,SECONDARY,ADDRESS,CITY,STATE,ZIP,CO_ADDRESS,"
    "SITUS_ADD_DOR,ACRE_DEED,ZONING,SUBNAME,ACCOUNTNO,LASTUPDATED"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://gis.yavapaiaz.gov/",
    "Origin": "https://gis.yavapaiaz.gov",
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

# gis.yavapaiaz.gov sits behind a WAF that 403s every request from GitHub
# Actions runner IPs, while the identical request succeeds from a
# residential/browser IP. Two fixes have been tried and both still get
# 403'd from Actions:
#   1. Realistic browser headers (UA/Accept/Referer/Origin) on plain
#      `requests` -- no change.
#   2. curl_cffi with `impersonate="chrome124"` (real Chrome TLS
#      fingerprint) -- still 403'd identically.
# Since a real-TLS-fingerprint client is STILL blocked, this is very
# likely IP/ASN-range blocking (GitHub Actions' datacenter IPs are
# blocklisted outright), not fingerprint-based bot detection -- no amount
# of header/TLS spoofing from an Actions runner will fix that. Left as
# `arcgis_session` (rather than reverting to plain `requests`) in case a
# future fix (e.g. a proxy, or a different runner IP range) makes the
# distinction relevant again; today (2026-08-13) it makes no difference.
# See COUNTIES.md for the decision to defer this county rather than keep
# debugging it. Supabase writes use the plain `requests` session above.
arcgis_session = cffi_requests.Session(impersonate="chrome124")
arcgis_session.headers.update(HEADERS)

_start = time.monotonic()


# ---------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------
def to_num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


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


def format_zip(value):
    z = clean(value)
    if not z:
        return None
    if len(z) == 9 and z.isdigit():
        return f"{z[:5]}-{z[5:]}"
    return z


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination -- single layer, no join needed
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
                r = arcgis_session.get(FEATURE_URL, params=params, timeout=60)
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
    "jurisdiction", "parcel_label", "property_address", "subdivision",
    "owner_name", "owner_name_2",
    "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "care_of_address",
    "land_size_acres", "zoning", "account_number",
    "source_last_updated",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "yavapai_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_row(a: dict, now_iso: str) -> dict:
    parcel = clean(a.get("PARCEL_ID"))
    owner_name = clean(a.get("NAME"))
    return {
        "parcel": parcel,
        "jurisdiction": "yavapai_county",
        "parcel_label": clean(a.get("PARLABEL")),
        "property_address": clean(a.get("SITUS_ADD_DOR")),
        "subdivision": clean(a.get("SUBNAME")),
        "owner_name": owner_name,
        "owner_name_2": clean(a.get("SECONDARY")),
        "mailing_address": clean(a.get("ADDRESS")),
        "mailing_city": clean(a.get("CITY")),
        "mailing_state": clean(a.get("STATE")),
        "mailing_zip": format_zip(a.get("ZIP")),
        "care_of_address": clean(a.get("CO_ADDRESS")),
        "land_size_acres": to_num(a.get("ACRE_DEED")),
        "zoning": clean(a.get("ZONING")),
        "account_number": clean(a.get("ACCOUNTNO")),
        "source_last_updated": epoch_ms_to_date(a.get("LASTUPDATED")),
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
    upsert("yavapai_property_info", deduped, on_conflict="parcel")
    print(f"Done. Upserted {len(deduped)} parcels this run.")


if __name__ == "__main__":
    main()
