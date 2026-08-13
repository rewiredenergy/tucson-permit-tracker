"""
Santa Cruz County Property Tracker
------------------------------------
Builds a per-parcel property profile for Santa Cruz County, Arizona,
entirely from two public bulk ArcGIS FeatureServer layers published by
the county's GIS/Assessor's office (the same backend that powers the
county's own "Parcel Search" map at
experience.arcgis.com/experience/ffed7d806f7a47239824b6412748c7f3):

1. Parcels_Tile -- master parcel roster: owner name, mailing address,
   site address, legal description, and land/improvement/full-cash/
   limited assessed values. ~43k parcels countywide.
2. Buildings_Tile -- building footprints with square footage, floor
   count, property class ("Residential"/"Commercial"/...), and a
   building-style description (e.g. "Ranch 1 Story"). Joined to
   Parcels_Tile by parcel number (APN). ~27k buildings countywide; a
   parcel with more than one building keeps its largest as "primary".

Unlike Pima County's property_info_tracker.py, this does NOT need a
per-parcel rate-limited API loop -- both layers are public and pulled in
a handful of large paginated bulk queries (2000 rows/page), so the
ENTIRE county is re-synced every run in a few minutes. The run is still
time-boxed (MAX_RUNTIME_MINUTES) as a safety net, and every upsert batch
is normalized to an identical column set and de-duplicated by parcel
before being sent to Supabase -- lessons learned the hard way from
Pima's property_info_tracker.py (PGRST102 "All object keys must match"
and Postgres 21000 "ON CONFLICT DO UPDATE command cannot affect row a
second time").

Two fields the user might want have no free public source and are left
off every row: year built and interior room/bath counts. The county's
official parcel-detail portal (parcelsearch.santacruzcountyaz.gov) does
show a year-built figure per building, but it's a classic ASP.NET
webforms page (__VIEWSTATE/__EVENTVALIDATION postback, one parcel per
request) -- a per-parcel enrichment pass could be added later the same
way Pima's assessor detail API is walked in property_info_tracker.py,
but that's a slower, separate follow-up, not part of this bulk sync.

COORDINATES: Parcels_Tile's attribute table has no LATITUDE/LONGITUDE
(or X/Y) fields -- confirmed via the layer's field list. Rather than
pull full polygon geometry, the Parcels_Tile fetch below asks ArcGIS to
compute a per-feature centroid server-side with
returnCentroid=true&outSR=4326 (combined with returnGeometry=false so
the full polygon is never transferred). Verified live against this
exact layer: a two-row test query returned
{"centroid": {"x": -110.75..., "y": 31.54...}} per feature, i.e.
x=longitude, y=latitude in WGS84 decimal degrees, correctly inside
Santa Cruz County. fetch_all_features() merges that centroid into each
row's attributes dict under synthetic keys (_LATITUDE/_LONGITUDE,
underscore-prefixed so they can't collide with a real ArcGIS field
name) when called with with_centroid=True. Buildings_Tile doesn't need
its own centroid -- every row is already joined back to its parcel's
coordinates in build_row().

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 60 (this job normally
                               finishes in a few minutes -- this is a
                               safety net, not a real budget like Pima's)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FEATURE_SERVER = "https://services1.arcgis.com/ZrefO5k0ipEAOFhn/arcgis/rest/services"
PARCELS_URL = f"{FEATURE_SERVER}/Parcels_Tile/FeatureServer/0/query"
BUILDINGS_URL = f"{FEATURE_SERVER}/Buildings_Tile/FeatureServer/0/query"

PAGE_SIZE = 2000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredSantaCruzPropertyTracker/1.0",
    "Accept": "application/json",
}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)
MAX_RUNTIME_MINUTES = float(os.environ.get("MAX_RUNTIME_MINUTES") or 60)

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
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination
# ---------------------------------------------------------------
def fetch_all_features(url: str, out_fields: str, order_by: str, label: str,
                        with_centroid: bool = False) -> list:
    features = []
    offset = 0
    while True:
        elapsed_min = (time.monotonic() - _start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"  Reached runtime budget ({MAX_RUNTIME_MINUTES} min) mid-fetch of "
                  f"{label} -- stopping with {len(features)} rows so far.")
            break
        params = {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": order_by,
            "f": "json",
        }
        if with_centroid:
            params["returnCentroid"] = "true"
            params["outSR"] = "4326"
        last_err = None
        d = None
        for attempt in range(1, 4):
            try:
                r = session.get(url, params=params, timeout=60)
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
            raise RuntimeError(f"{label} fetch failed at offset {offset}: {last_err}")
        feats = d.get("features") or []
        if not feats:
            break
        for f in feats:
            attrs = f["attributes"]
            if with_centroid:
                centroid = f.get("centroid") or {}
                attrs["_LONGITUDE"] = centroid.get("x")
                attrs["_LATITUDE"] = centroid.get("y")
            features.append(attrs)
        if len(features) % 10000 < PAGE_SIZE:
            print(f"  {label}: fetched {len(features)} rows so far")
        offset += PAGE_SIZE
        if len(feats) < PAGE_SIZE:
            break
    return features


def fetch_parcels() -> dict:
    print("Fetching Parcels_Tile (owner, mailing address, values, coordinates)...")
    fields = ("APN,SITEADDR,OWNERNAME1,OWNERNAME2,MAIL,MAIL2,MAIL3,MAIL4,"
              "LEGALDESCRIPTION,LANDVAL,IMPVAL,FCV,FCVASSESSE,LPV,LPVASSESSE,"
              "SALEP,DEEDTYPE,Acreage,TaxYear")
    rows = fetch_all_features(PARCELS_URL, fields, "APN", "Parcels_Tile", with_centroid=True)
    by_apn = {}
    for a in rows:
        apn = clean(a.get("APN"))
        if apn:
            by_apn[apn] = a
    print(f"Parcels_Tile: {len(by_apn)} distinct parcels")
    return by_apn


def fetch_buildings() -> dict:
    print("Fetching Buildings_Tile (sqft, floor count, building type)...")
    fields = "APN,BLGAREA,FLOORCOUNT,PROPCODE,BUILDINGDESCRIPTION_1"
    rows = fetch_all_features(BUILDINGS_URL, fields, "APN", "Buildings_Tile")
    by_apn = {}
    for a in rows:
        apn = clean(a.get("APN"))
        if not apn:
            continue
        # A parcel can have more than one building (e.g. a guest house) --
        # keep the largest by BLGAREA as the "primary" structure.
        area = to_num(a.get("BLGAREA")) or 0
        existing = by_apn.get(apn)
        if existing is None or area > (to_num(existing.get("BLGAREA")) or 0):
            by_apn[apn] = a
    print(f"Buildings_Tile: {len(by_apn)} parcels with at least one building")
    return by_apn


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------

# Every column a row might set, EXCLUDING "parcel" (always present, it's
# the primary key). Every upserted row -- normal or a defensive error
# row -- gets padded to this exact column set: PostgREST's bulk upsert
# rejects a batch whose objects don't all have EXACTLY the same keys
# (PGRST102 "All object keys must match"), the same crash Pima's
# property_info_tracker.py hit in production before this fix was added
# there. jurisdiction defaults to "santa_cruz_county" rather than None
# since the column is NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "owner_name", "mailing_address",
    "legal_description", "land_value", "improvement_value",
    "full_cash_value", "full_cash_assessed", "limited_value",
    "limited_assessed", "sale_price", "deed_type", "acreage", "tax_year",
    "interior_sqft", "stories", "property_type", "building_description",
    "latitude", "longitude",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "santa_cruz_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_owner_name(a: dict) -> str:
    parts = [clean(a.get("OWNERNAME1")), clean(a.get("OWNERNAME2"))]
    return " ".join(p for p in parts if p) or None


def build_mailing_address(a: dict) -> str:
    parts = [clean(a.get("MAIL")), clean(a.get("MAIL2")), clean(a.get("MAIL3")), clean(a.get("MAIL4"))]
    return ", ".join(p for p in parts if p) or None


def build_row(apn: str, p: dict, b: dict, now_iso: str) -> dict:
    owner_name = build_owner_name(p)
    row = {
        "parcel": apn,
        "jurisdiction": "santa_cruz_county",
        "property_address": clean(p.get("SITEADDR")),
        "owner_name": owner_name,
        "mailing_address": build_mailing_address(p),
        "legal_description": clean(p.get("LEGALDESCRIPTION")),
        "land_value": to_num(p.get("LANDVAL")),
        "improvement_value": to_num(p.get("IMPVAL")),
        "full_cash_value": to_num(p.get("FCV")),
        "full_cash_assessed": to_num(p.get("FCVASSESSE")),
        "limited_value": to_num(p.get("LPV")),
        "limited_assessed": to_num(p.get("LPVASSESSE")),
        "sale_price": to_num(p.get("SALEP")),
        "deed_type": clean(p.get("DEEDTYPE")),
        "acreage": to_num(p.get("Acreage")),
        "tax_year": clean(p.get("TaxYear")),
        "latitude": to_num(p.get("_LATITUDE")),
        "longitude": to_num(p.get("_LONGITUDE")),
        # No owner on file (vacant land, forest land, right-of-way, etc.)
        # is a normal, expected outcome here -- not a scrape failure.
        "status": "enriched" if owner_name else "no_owner_data",
        "enriched_at": now_iso,
        "updated_at": now_iso,
        "raw": {"parcel": p, "building": b},
    }
    if b:
        row.update({
            "interior_sqft": to_num(b.get("BLGAREA")),
            "stories": to_num(b.get("FLOORCOUNT")),
            "property_type": clean(b.get("PROPCODE")),
            "building_description": clean(b.get("BUILDINGDESCRIPTION_1")),
        })
    return row


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

    parcels = fetch_parcels()
    buildings = fetch_buildings()

    apns = list(parcels.keys())
    if SAMPLE_LIMIT:
        apns = apns[:SAMPLE_LIMIT]

    rows = []
    for apn in apns:
        try:
            rows.append(normalize_row(build_row(apn, parcels[apn], buildings.get(apn), now_iso)))
        except Exception as e:  # noqa: BLE001
            print(f"  warning: row build failed for {apn}: {e}")
            rows.append(normalize_row({
                "parcel": apn, "status": "error",
                "error_note": str(e)[:300], "updated_at": now_iso,
            }))

    # Defensive de-dup, keeping the LAST row seen per parcel. apns already
    # come from a dict's keys so this should be a no-op in practice, but
    # Postgres rejects an upsert batch containing the same conflict key
    # (parcel) twice ("ON CONFLICT DO UPDATE command cannot affect row a
    # second time") -- the same failure Pima's tracker hit in production.
    # Cheap insurance against ever hitting that again.
    deduped = list({r["parcel"]: r for r in rows}.values())

    print(f"Upserting {len(deduped)} parcels to Supabase...")
    upsert("santa_cruz_property_info", deduped, on_conflict="parcel")
    print(f"Done. Upserted {len(deduped)} parcels this run.")


if __name__ == "__main__":
    main()
"""
Santa Cruz County Property Tracker
------------------------------------
Builds a per-parcel property profile for Santa Cruz County, Arizona,
entirely from two public bulk ArcGIS FeatureServer layers published by
the county's GIS/Assessor's office (the same backend that powers the
county's own "Parcel Search" map at
experience.arcgis.com/experience/ffed7d806f7a47239824b6412748c7f3):

  1. Parcels_Tile -- master parcel roster: owner name, mailing address,
     site address, legal description, and land/improvement/full-cash/
     limited assessed values. ~43k parcels countywide.
  2. Buildings_Tile -- building footprints with square footage, floor
     count, property class ("Residential"/"Commercial"/...), and a
     building-style description (e.g. "Ranch 1 Story"). Joined to
     Parcels_Tile by parcel number (APN). ~27k buildings countywide; a
     parcel with more than one building keeps its largest as "primary".

Unlike Pima County's property_info_tracker.py, this does NOT need a
per-parcel rate-limited API loop -- both layers are public and pulled in
a handful of large paginated bulk queries (2000 rows/page), so the
ENTIRE county is re-synced every run in a few minutes. The run is still
time-boxed (MAX_RUNTIME_MINUTES) as a safety net, and every upsert batch
is normalized to an identical column set and de-duplicated by parcel
before being sent to Supabase -- lessons learned the hard way from
Pima's property_info_tracker.py (PGRST102 "All object keys must match"
and Postgres 21000 "ON CONFLICT DO UPDATE command cannot affect row a
second time").

Two fields the user might want have no free public source and are left
off every row: year built and interior room/bath counts. The county's
official parcel-detail portal (parcelsearch.santacruzcountyaz.gov) does
show a year-built figure per building, but it's a classic ASP.NET
webforms page (__VIEWSTATE/__EVENTVALIDATION postback, one parcel per
request) -- a per-parcel enrichment pass could be added later the same
way Pima's assessor detail API is walked in property_info_tracker.py,
but that's a slower, separate follow-up, not part of this bulk sync.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "500" to stop early (test runs)
  MAX_RUNTIME_MINUTES         optional; default 60 (this job normally
                               finishes in a few minutes -- this is a
                               safety net, not a real budget like Pima's)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FEATURE_SERVER = "https://services1.arcgis.com/ZrefO5k0ipEAOFhn/arcgis/rest/services"
PARCELS_URL = f"{FEATURE_SERVER}/Parcels_Tile/FeatureServer/0/query"
BUILDINGS_URL = f"{FEATURE_SERVER}/Buildings_Tile/FeatureServer/0/query"

PAGE_SIZE = 2000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredSantaCruzPropertyTracker/1.0",
    "Accept": "application/json",
}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)
MAX_RUNTIME_MINUTES = float(os.environ.get("MAX_RUNTIME_MINUTES") or 60)

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
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def clean(value):
    v = (str(value) if value is not None else "").strip()
    return v or None


# ---------------------------------------------------------------
# Bulk ArcGIS FeatureServer pagination
# ---------------------------------------------------------------
def fetch_all_features(url: str, out_fields: str, order_by: str, label: str) -> list:
    features = []
    offset = 0
    while True:
        elapsed_min = (time.monotonic() - _start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"  Reached runtime budget ({MAX_RUNTIME_MINUTES} min) mid-fetch of "
                  f"{label} -- stopping with {len(features)} rows so far.")
            break
        params = {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": order_by,
            "f": "json",
        }
        last_err = None
        d = None
        for attempt in range(1, 4):
            try:
                r = session.get(url, params=params, timeout=60)
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
            raise RuntimeError(f"{label} fetch failed at offset {offset}: {last_err}")
        feats = d.get("features") or []
        if not feats:
            break
        features.extend(f["attributes"] for f in feats)
        if len(features) % 10000 < PAGE_SIZE:
            print(f"  {label}: fetched {len(features)} rows so far")
        offset += PAGE_SIZE
        if len(feats) < PAGE_SIZE:
            break
    return features


def fetch_parcels() -> dict:
    print("Fetching Parcels_Tile (owner, mailing address, values)...")
    fields = ("APN,SITEADDR,OWNERNAME1,OWNERNAME2,MAIL,MAIL2,MAIL3,MAIL4,"
              "LEGALDESCRIPTION,LANDVAL,IMPVAL,FCV,FCVASSESSE,LPV,LPVASSESSE,"
              "SALEP,DEEDTYPE,Acreage,TaxYear")
    rows = fetch_all_features(PARCELS_URL, fields, "APN", "Parcels_Tile")
    by_apn = {}
    for a in rows:
        apn = clean(a.get("APN"))
        if apn:
            by_apn[apn] = a
    print(f"Parcels_Tile: {len(by_apn)} distinct parcels")
    return by_apn


def fetch_buildings() -> dict:
    print("Fetching Buildings_Tile (sqft, floor count, building type)...")
    fields = "APN,BLGAREA,FLOORCOUNT,PROPCODE,BUILDINGDESCRIPTION_1"
    rows = fetch_all_features(BUILDINGS_URL, fields, "APN", "Buildings_Tile")
    by_apn = {}
    for a in rows:
        apn = clean(a.get("APN"))
        if not apn:
            continue
        # A parcel can have more than one building (e.g. a guest house) --
        # keep the largest by BLGAREA as the "primary" structure.
        area = to_num(a.get("BLGAREA")) or 0
        existing = by_apn.get(apn)
        if existing is None or area > (to_num(existing.get("BLGAREA")) or 0):
            by_apn[apn] = a
    print(f"Buildings_Tile: {len(by_apn)} parcels with at least one building")
    return by_apn


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------

# Every column a row might set, EXCLUDING "parcel" (always present, it's
# the primary key). Every upserted row -- normal or a defensive error
# row -- gets padded to this exact column set: PostgREST's bulk upsert
# rejects a batch whose objects don't all have EXACTLY the same keys
# (PGRST102 "All object keys must match"), the same crash Pima's
# property_info_tracker.py hit in production before this fix was added
# there. jurisdiction defaults to "santa_cruz_county" rather than None
# since the column is NOT NULL.
ROW_COLUMNS = [
    "jurisdiction", "property_address", "owner_name", "mailing_address",
    "legal_description", "land_value", "improvement_value",
    "full_cash_value", "full_cash_assessed", "limited_value",
    "limited_assessed", "sale_price", "deed_type", "acreage", "tax_year",
    "interior_sqft", "stories", "property_type", "building_description",
    "status", "error_note", "raw", "enriched_at", "updated_at",
]


def normalize_row(row: dict) -> dict:
    out = {"parcel": row["parcel"], "jurisdiction": row.get("jurisdiction") or "santa_cruz_county"}
    for col in ROW_COLUMNS:
        if col != "jurisdiction":
            out[col] = row.get(col)
    return out


def build_owner_name(a: dict) -> str:
    parts = [clean(a.get("OWNERNAME1")), clean(a.get("OWNERNAME2"))]
    return " ".join(p for p in parts if p) or None


def build_mailing_address(a: dict) -> str:
    parts = [clean(a.get("MAIL")), clean(a.get("MAIL2")), clean(a.get("MAIL3")), clean(a.get("MAIL4"))]
    return ", ".join(p for p in parts if p) or None


def build_row(apn: str, p: dict, b: dict, now_iso: str) -> dict:
    owner_name = build_owner_name(p)
    row = {
        "parcel": apn,
        "jurisdiction": "santa_cruz_county",
        "property_address": clean(p.get("SITEADDR")),
        "owner_name": owner_name,
        "mailing_address": build_mailing_address(p),
        "legal_description": clean(p.get("LEGALDESCRIPTION")),
        "land_value": to_num(p.get("LANDVAL")),
        "improvement_value": to_num(p.get("IMPVAL")),
        "full_cash_value": to_num(p.get("FCV")),
        "full_cash_assessed": to_num(p.get("FCVASSESSE")),
        "limited_value": to_num(p.get("LPV")),
        "limited_assessed": to_num(p.get("LPVASSESSE")),
        "sale_price": to_num(p.get("SALEP")),
        "deed_type": clean(p.get("DEEDTYPE")),
        "acreage": to_num(p.get("Acreage")),
        "tax_year": clean(p.get("TaxYear")),
        # No owner on file (vacant land, forest land, right-of-way, etc.)
        # is a normal, expected outcome here -- not a scrape failure.
        "status": "enriched" if owner_name else "no_owner_data",
        "enriched_at": now_iso,
        "updated_at": now_iso,
        "raw": {"parcel": p, "building": b},
    }
    if b:
        row.update({
            "interior_sqft": to_num(b.get("BLGAREA")),
            "stories": to_num(b.get("FLOORCOUNT")),
            "property_type": clean(b.get("PROPCODE")),
            "building_description": clean(b.get("BUILDINGDESCRIPTION_1")),
        })
    return row


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

    parcels = fetch_parcels()
    buildings = fetch_buildings()

    apns = list(parcels.keys())
    if SAMPLE_LIMIT:
        apns = apns[:SAMPLE_LIMIT]

    rows = []
    for apn in apns:
        try:
            rows.append(normalize_row(build_row(apn, parcels[apn], buildings.get(apn), now_iso)))
        except Exception as e:  # noqa: BLE001
            print(f"  warning: row build failed for {apn}: {e}")
            rows.append(normalize_row({
                "parcel": apn, "status": "error",
                "error_note": str(e)[:300], "updated_at": now_iso,
            }))

    # Defensive de-dup, keeping the LAST row seen per parcel. apns already
    # come from a dict's keys so this should be a no-op in practice, but
    # Postgres rejects an upsert batch containing the same conflict key
    # (parcel) twice ("ON CONFLICT DO UPDATE command cannot affect row a
    # second time") -- the same failure Pima's tracker hit in production.
    # Cheap insurance against ever hitting that again.
    deduped = list({r["parcel"]: r for r in rows}.values())

    print(f"Upserting {len(deduped)} parcels to Supabase...")
    upsert("santa_cruz_property_info", deduped, on_conflict="parcel")
    print(f"Done. Upserted {len(deduped)} parcels this run.")


if __name__ == "__main__":
    main()
