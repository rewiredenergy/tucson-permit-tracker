"""
Property Info Tracker
----------------------
Builds a rich per-property profile for Pima County homes, entirely from
free / public sources (no Zillow, no MLS, no login-gated APIs):

  1. Pima County GIS "Parcels - Regional" layer -- master roster of every
     parcel in the county (address, lat/lon). Seeded in bulk, cheaply.
  2. Pima County Assessor parcel-detail API -- interior sqft, year built,
     stories, roof material, room/bath-fixture counts, lot size, assessed
     value. (Same API used by sales_scraper.py.)
  3. Pima County GIS "Zoning - All Jurisdictions" layer -- zoning code via
     a point-in-polygon spatial query on the parcel's coordinates.
  4. Pima County Treasurer property-inquiry page -- the most recent year's
     total tax bill (parsed out of the server-rendered HTML).
  5. A *modeled* projected-electricity-bill estimate built from Tucson
     Electric Power's public "Basic" residential rate schedule, scaled by
     the home's interior sqft. This is NOT a scraped/measured value --
     see electricity_estimate_basis on each row.

Two fields the user asked for have no free public source and are left
null on every row: beds (county records track total rooms and bath
fixtures for valuation, not bedroom/bathroom counts) and HOA yes/no/
name/phone (that lives in MLS listings or recorded CC&Rs, not in any
free county API).

This is a FULL-COUNTY SWEEP (~300k+ residential parcels). One run will
not come close to finishing -- it works a fixed time budget per run
(MAX_RUNTIME_MINUTES) and leaves the rest in the queue (status='pending')
for the next scheduled run to continue. Parcels already seen by the
sales tracker or the permit tracker are enriched first, since those are
the properties actively feeding Knockzy leads.

Environment variables (GitHub Secrets -- never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT                optional; e.g. "25" for a small test run
  MAX_RUNTIME_MINUTES         optional; default 320 (stay under GitHub
                               Actions' 360-minute default job timeout)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

ASR_BASE = "https://www.asr.pima.gov"
DETAIL_URL = ASR_BASE + "/AssessorSiteData/api/get/parceldetails/"
TAXYEAR_URL = ASR_BASE + "/AssessorSiteData/api/get/dynamicdetails/?function=GetPublicWebYear"

ROSTER_URL = "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/12/query"
ZONING_URL = "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/Boundaries2/MapServer/4/query"
TAX_URL = "https://www.to.pima.gov/propertyInquiry/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredPropertyInfoTracker/1.0",
    "Accept": "application/json",
}

REQUEST_DELAY = 1.0
MAX_RETRIES = 4
RATE_LIMIT_COOLDOWN = 45
MAX_CONSECUTIVE_429S = 8
CIRCUIT_BREAKER_COOLDOWN = 300

ROSTER_PAGE_SIZE = 2000
QUEUE_BATCH_SIZE = 50
FLUSH_EVERY = 100

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)
MAX_RUNTIME_MINUTES = float(os.environ.get("MAX_RUNTIME_MINUTES") or 320)
SKIP_SEED = (os.environ.get("SKIP_SEED") or "").strip().lower() in ("1", "true", "yes")

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

_consecutive_429s = 0


# ---------------------------------------------------------------
# TEP "Basic" residential rate schedule (https://www.tep.com/basic/,
# read 2026) -- used only to MODEL a projected electricity bill, never
# scraped per-property. Update these constants if TEP's public rates
# change.
# ---------------------------------------------------------------
TEP_SERVICE_CHARGE = 15.00  # single-phase monthly service charge
TEP_SUMMER_TIERS = [(500, 0.1304), (500, 0.1501), (float("inf"), 0.1573)]  # May-Sep
TEP_WINTER_TIERS = [(500, 0.1264), (500, 0.1460), (float("inf"), 0.1532)]  # Oct-Apr
# TEP's own 2026 rate-case filing cites a median residential customer
# using 638 kWh/month on the Basic plan -- used as the scaling baseline.
TEP_BASELINE_MONTHLY_KWH = 638
TEP_BASELINE_SQFT = 1800


def _tier_cost(kwh: float, tiers) -> float:
    remaining = kwh
    cost = 0.0
    for tier_kwh, rate in tiers:
        used = min(remaining, tier_kwh)
        cost += used * rate
        remaining -= used
        if remaining <= 0:
            break
    return cost


def estimate_electricity(sqft):
    """Rough projected ANNUAL electricity bill. Modeled, not measured --
    see the docstring above. Returns (amount, basis_text) or (None, None)."""
    if not sqft or sqft <= 0:
        return None, None
    monthly_kwh = TEP_BASELINE_MONTHLY_KWH * (sqft / TEP_BASELINE_SQFT)
    summer_month = _tier_cost(monthly_kwh, TEP_SUMMER_TIERS) + TEP_SERVICE_CHARGE
    winter_month = _tier_cost(monthly_kwh, TEP_WINTER_TIERS) + TEP_SERVICE_CHARGE
    annual = summer_month * 5 + winter_month * 7
    basis = (
        "Modeled estimate, not a measured bill: TEP's public Basic residential "
        "tiered rate schedule (tep.com/basic, 2026) applied to a monthly kWh "
        "figure scaled from TEP's own published median-customer usage "
        f"({TEP_BASELINE_MONTHLY_KWH} kWh/mo) by this home's interior sqft vs "
        f"a {TEP_BASELINE_SQFT} sqft baseline."
    )
    return round(annual, 2), basis


# ---------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------
def _first(x):
    if isinstance(x, list):
        return x[0] if x else {}
    return x or {}


def to_num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def to_int(value):
    try:
        return int(float(value)) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def get_current_taxyear() -> int:
    try:
        r = session.get(TAXYEAR_URL, timeout=30)
        val = r.json()
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return int(val[0].get("PublicWebYear"))
        return int(str(val).strip().strip('"'))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).year + 1


# ---------------------------------------------------------------
# Assessor parcel detail (rate-limited, circuit-breaker protected --
# same pattern proven out in sales_scraper.py)
# ---------------------------------------------------------------
def fetch_parcel_detail(parcel: str, taxyear: int) -> dict:
    global _consecutive_429s
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.post(DETAIL_URL,
                             headers={"Content-Type": "application/json"},
                             data=json.dumps({"parcel": parcel, "taxyear": taxyear}),
                             timeout=60)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and "PropertyInfo" in d:
                    _consecutive_429s = 0
                    return d
                raise RuntimeError(f"unexpected response: {str(d)[:100]}")
            if r.status_code == 429:
                _consecutive_429s += 1
                if _consecutive_429s >= MAX_CONSECUTIVE_429S:
                    print(f"    rate limited {_consecutive_429s}x in a row — "
                          f"cooling down {CIRCUIT_BREAKER_COOLDOWN}s")
                    time.sleep(CIRCUIT_BREAKER_COOLDOWN)
                    _consecutive_429s = 0
                else:
                    time.sleep(RATE_LIMIT_COOLDOWN)
                raise RuntimeError("HTTP 429 (rate limited)")
            raise RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if "429" not in str(e):
                time.sleep(3 * attempt)
    raise RuntimeError(f"parcel detail failed: {last_err}")


# ---------------------------------------------------------------
# Zoning (point-in-polygon spatial query, public ArcGIS MapServer)
# ---------------------------------------------------------------
def fetch_zoning(x, y):
    if x is None or y is None:
        return None
    params = {
        "geometry": json.dumps({"x": x, "y": y, "spatialReference": {"wkid": 2868}}),
        "geometryType": "esriGeometryPoint",
        "inSR": "2868",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZONING,ZONE,JURISDICTION",
        "returnGeometry": "false",
        "f": "json",
    }
    last_err = None
    for attempt in range(1, 3):
        try:
            r = session.get(ZONING_URL, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
            feats = d.get("features") or []
            if not feats:
                return None
            a = feats[0]["attributes"]
            return {
                "zoning": a.get("ZONING"),
                "zoning_base": a.get("ZONE"),
                "zoning_jurisdiction": a.get("JURISDICTION"),
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"zoning query failed: {last_err}")


# ---------------------------------------------------------------
# Treasurer tax bill (server-rendered HTML page, parsed with regex --
# confirmed the values are present in the raw response, no JS needed)
# ---------------------------------------------------------------
TOTAL_TAX_RE = re.compile(r'>TOTAL TAX</div>\s*<div[^>]*>([\d,]+\.\d+)</div>')
TAX_YEAR_RE = re.compile(r'>TAX YEAR</div>\s*<div[^>]*>(\d{4})</div>')


def fetch_annual_tax(parcel: str):
    last_err = None
    for attempt in range(1, 3):
        try:
            r = session.get(TAX_URL, params={"stateCode": parcel}, timeout=30)
            r.raise_for_status()
            html = r.text
            tax_m = TOTAL_TAX_RE.search(html)
            if not tax_m:
                return None, None
            year_m = TAX_YEAR_RE.search(html)
            amount = float(tax_m.group(1).replace(",", ""))
            year = int(year_m.group(1)) if year_m else None
            return amount, year
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"tax lookup failed: {last_err}")


# ---------------------------------------------------------------
# Supabase (network-retry wrapper -- same pattern proven out in
# scraper.py / sales_scraper.py after the transient-timeout crashes)
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
                      f"(attempt {attempt}/5): {e} — retrying in {wait}s")
                time.sleep(wait)
        if last_err is not None:
            raise RuntimeError(f"Supabase write to {table} failed after 5 attempts: {last_err}")


def patch_with_retry(url: str, body: dict) -> None:
    last_err = None
    for attempt in range(1, 6):
        try:
            r = session.patch(url, headers=SUPABASE_HEADERS, data=json.dumps(body), timeout=60)
            if r.status_code >= 300:
                raise RuntimeError(f"Supabase PATCH failed ({r.status_code}): {r.text[:300]}")
            return
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            wait = 5 * attempt
            print(f"  Supabase PATCH network error (attempt {attempt}/5): {e} — retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Supabase PATCH failed after 5 attempts: {last_err}")


def get_distinct_parcels(table: str, column: str) -> list:
    parcels = set()
    offset, chunk = 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/{table}"
               f"?select={column}&{column}=not.is.null&limit={chunk}&offset={offset}")
        r = session.get(url, headers=SUPABASE_HEADERS, timeout=60)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            v = (row.get(column) or "").strip()
            if v:
                parcels.add(v)
        if len(rows) < chunk:
            return list(parcels)
        offset += chunk


# ---------------------------------------------------------------
# Roster seeding -- bulk pull of every parcel in the county from the
# public GIS layer. Cheap (no per-parcel rate limiting -- it's a
# handful of large paginated queries, not one request per parcel).
# Safe to re-run: only touches identity/location columns, never the
# enrichment or status columns, so it won't undo progress.
# ---------------------------------------------------------------
def seed_parcel_roster() -> int:
    print("Seeding parcel roster from Pima County GIS...")
    offset, total = 0, 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "PARCEL,ADDRESS_OL,ZIP,LAT,LON",
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": ROSTER_PAGE_SIZE,
            "orderByFields": "PARCEL",
            "f": "json",
        }
        r = session.get(ROSTER_URL, params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        feats = d.get("features") or []
        if not feats:
            break
        batch = []
        for f in feats:
            a = f["attributes"]
            parcel = (a.get("PARCEL") or "").strip()
            if not parcel:
                continue
            batch.append({
                "parcel": parcel,
                "jurisdiction": "pima_county",
                "property_address": (a.get("ADDRESS_OL") or "").strip() or None,
                "zip": (a.get("ZIP") or "").strip() or None,
                "latitude": a.get("LAT"),
                "longitude": a.get("LON"),
            })
        if batch:
            upsert("property_info", batch, on_conflict="parcel")
            total += len(batch)
        if total % 20000 < ROSTER_PAGE_SIZE:
            print(f"  seeded {total} parcels so far")
        offset += ROSTER_PAGE_SIZE
        if len(feats) < ROSTER_PAGE_SIZE:
            break
    print(f"Roster seed complete: {total} parcels")
    return total


def tag_priority_parcels() -> None:
    print("Tagging priority parcels already tracked by the sales/permit scrapers...")
    for table, column, label in (
        ("property_sales", "parcel", "sales_tracker"),
        ("solar_permits", "parcel_number", "permit_tracker"),
    ):
        try:
            parcels = get_distinct_parcels(table, column)
        except Exception as e:  # noqa: BLE001
            print(f"  warning: couldn't read {table}.{column}: {e}")
            continue
        print(f"  {len(parcels)} distinct parcels from {table}")
        for i in range(0, len(parcels), 200):
            chunk = parcels[i:i + 200]
            in_list = ",".join(chunk)
            url = (f"{SUPABASE_URL}/rest/v1/property_info"
                   f"?parcel=in.({in_list})&source_priority=is.null&status=eq.pending")
            try:
                patch_with_retry(url, {"source_priority": label})
            except Exception as e:  # noqa: BLE001
                print(f"  warning: tagging batch failed ({label}): {e}")


def get_queue_batch(limit: int) -> list:
    rows = []
    url = (f"{SUPABASE_URL}/rest/v1/property_info"
           f"?status=eq.pending&source_priority=not.is.null"
           f"&select=parcel,property_address,zip,latitude,longitude&limit={limit}")
    r = session.get(url, headers=SUPABASE_HEADERS, timeout=60)
    r.raise_for_status()
    rows += r.json()
    if len(rows) < limit:
        remaining = limit - len(rows)
        url2 = (f"{SUPABASE_URL}/rest/v1/property_info"
                f"?status=eq.pending&source_priority=is.null"
                f"&select=parcel,property_address,zip,latitude,longitude&limit={remaining}")
        r2 = session.get(url2, headers=SUPABASE_HEADERS, timeout=60)
        r2.raise_for_status()
        rows += r2.json()
    return rows


# ---------------------------------------------------------------
# Row building
# ---------------------------------------------------------------
def _situs_address(situs: dict) -> str:
    addr = " ".join(str(situs.get(k) or "").strip() for k in
                    ("StreetNumber", "StreetDirection", "StreetName")).strip()
    city = (situs.get("City") or "").strip()
    return (addr + (", " + city if city else "")) or None


def build_row(roster: dict, detail: dict, zoning, tax_amount, tax_year, now_iso: str) -> dict:
    pi = _first(detail.get("PropertyInfo"))
    va = _first(detail.get("ValuationArea"))
    nv = _first(detail.get("NoticedValuationData"))
    rc = _first(detail.get("ResidentialChar"))
    mailing = _first(detail.get("Mailing"))
    situs_list = detail.get("SITUS") or []
    situs = situs_list[0] if situs_list else {}

    is_residential = bool(rc)

    row = {
        "parcel": roster["parcel"],
        "jurisdiction": "pima_county",
        "property_address": roster.get("property_address") or _situs_address(situs),
        "zip": roster.get("zip"),
        "latitude": va.get("Latitude") or roster.get("latitude"),
        "longitude": va.get("Longitude") or roster.get("longitude"),
        "owner_name": (mailing.get("ParcelOwner") or "").strip() or None,
        "property_type": rc.get("PropertyType") if is_residential else None,
        "parcel_use_desc": pi.get("ParcelUseDesc"),
        "status": "enriched" if is_residential else "not_residential",
        "enriched_at": now_iso,
        "updated_at": now_iso,
        "raw": {"detail": detail, "zoning": zoning},
    }

    if is_residential:
        sqft = to_num(rc.get("SQFT"))
        fcv = to_num(nv.get("TotalFCV"))
        row.update({
            "interior_sqft": sqft,
            "total_rooms": to_int(rc.get("ROOMS")),
            "bath_fixtures": to_num(rc.get("BATHFIXTURES")),
            "lot_size_sqft": to_num(va.get("LandSqFt")),
            "stories": to_num(rc.get("STORIES")),
            "roof_material": rc.get("ROOF"),
            "wall_material": rc.get("WALLS"),
            "year_built": to_int(rc.get("YEAR")),
            "assessed_full_cash_value": fcv,
            "limited_assessed_value": to_num(nv.get("LimitedAssessed")),
            "price_per_sqft": round(fcv / sqft, 2) if fcv and sqft else None,
            "annual_tax_amount": tax_amount,
            "tax_year": tax_year,
        })
        elec, basis = estimate_electricity(sqft)
        row["projected_annual_electricity_bill"] = elec
        row["electricity_estimate_basis"] = basis
        if zoning:
            row.update(zoning)

    return row


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"Run started {now_iso}"
          + (f" — TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else "")
          + f" — runtime budget {MAX_RUNTIME_MINUTES} min")

    if SKIP_SEED:
        print("SKIP_SEED set — skipping roster seed and priority tagging.")
    else:
        seed_parcel_roster()
        tag_priority_parcels()

    taxyear = get_current_taxyear()
    print(f"Assessor tax year: {taxyear}")

    start = time.monotonic()
    processed = 0
    buffer = []

    def flush():
        nonlocal buffer, processed
        if not buffer:
            return
        upsert("property_info", buffer, on_conflict="parcel")
        processed += len(buffer)
        print(f"  saved batch of {len(buffer)} ({processed} total this run)")
        buffer = []

    while True:
        elapsed_min = (time.monotonic() - start) / 60
        if elapsed_min >= MAX_RUNTIME_MINUTES:
            print(f"Reached runtime budget ({MAX_RUNTIME_MINUTES} min) — stopping for this run.")
            break
        if SAMPLE_LIMIT and processed >= SAMPLE_LIMIT:
            break

        batch = get_queue_batch(QUEUE_BATCH_SIZE)
        if not batch:
            print("Queue is empty — full sweep complete!")
            break

        for roster in batch:
            if SAMPLE_LIMIT and (processed + len(buffer)) >= SAMPLE_LIMIT:
                break
            parcel = roster["parcel"]
            try:
                detail = fetch_parcel_detail(parcel, taxyear)
                rc = _first(detail.get("ResidentialChar"))
                if not rc:
                    buffer.append({
                        "parcel": parcel, "status": "not_residential",
                        "enriched_at": now_iso, "updated_at": now_iso,
                    })
                else:
                    va = _first(detail.get("ValuationArea"))
                    zoning = None
                    if va.get("X") is not None and va.get("Y") is not None:
                        try:
                            zoning = fetch_zoning(va["X"], va["Y"])
                        except Exception as e:  # noqa: BLE001
                            print(f"  zoning lookup failed for {parcel}: {e}")
                    tax_amount = tax_year_val = None
                    try:
                        tax_amount, tax_year_val = fetch_annual_tax(parcel)
                    except Exception as e:  # noqa: BLE001
                        print(f"  tax lookup failed for {parcel}: {e}")
                    buffer.append(build_row(roster, detail, zoning, tax_amount, tax_year_val, now_iso))
            except Exception as e:  # noqa: BLE001
                print(f"  warning: enrichment failed for {parcel}: {e}")
                buffer.append({
                    "parcel": parcel, "status": "error",
                    "error_note": str(e)[:300], "updated_at": now_iso,
                })
            if len(buffer) >= FLUSH_EVERY:
                flush()

    flush()
    print(f"Done. Enriched {processed} parcels this run.")


if __name__ == "__main__":
    main()
