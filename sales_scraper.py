"""
Pima County New-Homeowner Tracker
---------------------------------
Downloads the Pima County Assessor's nightly "Affidavits of Sales"
file (every recorded property sale this year), detects sales we
haven't seen before, enriches each new one with the buyer's name,
property address, and home characteristics from the Assessor's
parcel API, and stores everything in Supabase.

Environment variables (GitHub Secrets — never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT               optional; e.g. "25" for a small test run
  SALE_YEARS                 optional; e.g. "2024,2025,2026" to backfill
                             more years (default: current year only)
"""

import csv
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone

import requests

ASR_BASE = "https://www.asr.pima.gov"
SALES_URL = ASR_BASE + "/Downloads/Data/sales/{year}/SALE{year}.ZIP"
DETAIL_URL = ASR_BASE + "/AssessorSiteData/api/get/parceldetails/"
TAXYEAR_URL = ASR_BASE + "/AssessorSiteData/api/get/dynamicdetails/?function=GetPublicWebYear"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RewiredHomeownerTracker/1.0",
    "Accept": "application/json",
}

REQUEST_DELAY = 0.35
MAX_RETRIES = 4

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)
SALE_YEARS = [y.strip() for y in
              (os.environ.get("SALE_YEARS") or str(datetime.now(timezone.utc).year)).split(",")
              if y.strip()]

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------
# Assessor data
# ---------------------------------------------------------------
def download_sales_csv(year: str) -> list[dict]:
    """Download and parse SALE{year}.ZIP -> list of sale-row dicts."""
    url = SALES_URL.format(year=year)
    # Accept must allow binary content — the server returns 406 for JSON-only
    resp = session.get(url, timeout=120,
                       headers={"Accept": "*/*"})
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
    if not csv_name:
        raise RuntimeError(f"No CSV found inside {url}")
    text = zf.read(csv_name).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = [row for row in reader if row.get("Parcel")]
    print(f"  {year}: {len(rows)} sales in {csv_name}")
    return rows


def get_current_taxyear() -> int:
    try:
        r = session.get(TAXYEAR_URL, timeout=30)
        val = r.json()
        # response may be a bare number or string
        return int(str(val).strip().strip('"'))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).year + 1


def fetch_parcel_detail(parcel: str, taxyear: int) -> dict | None:
    """Owner name, addresses, and home characteristics for one parcel."""
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
                if isinstance(d, dict) and "Mailing" in d:
                    return d
                raise RuntimeError(f"unexpected response: {str(d)[:100]}")
            raise RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * attempt)
    print(f"  warning: parcel detail failed for {parcel}: {last_err}")
    return None


# ---------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------
def yn(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in ("yes", "y", "true", "1")


def month_to_date(value: str | None) -> str | None:
    """'202601' -> '2026-01-01'"""
    v = (value or "").strip()
    if len(v) == 6 and v.isdigit():
        return f"{v[:4]}-{v[4:]}-01"
    return None


def to_num(value: str | None):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def to_int(value):
    """Safely coerce values like 1, '1', or 1.0 to a plain integer."""
    try:
        return int(float(value)) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def build_row(sale: dict, detail: dict | None, now_iso: str) -> dict:
    parcel = sale["Parcel"].strip()
    seq = (sale.get("SequenceNum") or "").strip()
    row = {
        "sale_key": f"{parcel}_{seq}" if seq else f"{parcel}_{sale.get('RecordingDate','')}",
        "jurisdiction": "pima_county",
        "parcel": parcel,
        "sequence_num": seq or None,
        "sale_month": month_to_date(sale.get("SaleDate")),
        "recording_date": (sale.get("RecordingDate") or "").strip() or None,
        "sale_price": to_num(sale.get("SalePrice")),
        "property_type": sale.get("PropertyType") or None,
        "intended_use": sale.get("IntendedUse") or None,
        "deed_type": sale.get("Deed") or None,
        "financing": sale.get("Financing") or None,
        "validation_desc": sale.get("ValidationDescription") or None,
        "buyer_seller_related": sale.get("BuyerSellerRelated") or None,
        "has_solar": yn(sale.get("Solar")),
        "parcel_use": sale.get("ParcelUse") or None,
        "updated_at": now_iso,
        "raw": {"sale": sale},
    }
    if detail:
        mailing = detail.get("Mailing") or {}
        situs_list = detail.get("SITUS") or []
        situs = situs_list[0] if situs_list else {}
        rc = detail.get("ResidentialChar") or {}
        if isinstance(rc, list):
            rc = rc[0] if rc else {}
        mail_parts = [mailing.get(k) for k in ("Mail2", "Mail3", "Mail4", "Mail5")]
        zipc = (mailing.get("Zip") or "").strip()
        if zipc:
            mail_parts.append(zipc)
        row["owner_name"] = (mailing.get("ParcelOwner") or "").strip() or None
        row["mailing_address"] = ", ".join(p.strip() for p in mail_parts
                                           if p and p.strip()) or None
        addr = " ".join(str(situs.get(k) or "").strip() for k in
                        ("StreetNumber", "StreetDirection", "StreetName")).strip()
        city = (situs.get("City") or "").strip()
        row["property_address"] = (addr + (", " + city if city else "")) or None
        row["living_area_sqft"] = to_num(rc.get("SQFT"))
        row["year_built"] = to_int(rc.get("YEAR"))
        row["stories"] = to_int(rc.get("STORIES"))
        row["cooling"] = rc.get("COOL")
        row["heating"] = rc.get("HEAT")
        row["raw"]["parcel_detail"] = {
            "Mailing": mailing, "SITUS": situs_list, "ResidentialChar": rc,
        }
    return row


# ---------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------
def get_existing_sale_keys() -> set[str]:
    existing: set[str] = set()
    offset, chunk = 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/property_sales"
               f"?select=sale_key&limit={chunk}&offset={offset}")
        r = session.get(url, headers=SUPABASE_HEADERS, timeout=60)
        r.raise_for_status()
        rows = r.json()
        existing.update(x["sale_key"] for x in rows)
        if len(rows) < chunk:
            return existing
        offset += chunk


def upsert(table: str, rows: list[dict], on_conflict: str | None = None) -> None:
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), 500):
        r = session.post(url, headers=headers,
                         data=json.dumps(rows[i:i + 500]), timeout=120)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase write to {table} failed "
                               f"({r.status_code}): {r.text[:300]}")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"Run started {now_iso}"
          + (f" — TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else ""))

    print("Loading known sales from Supabase...")
    existing = get_existing_sale_keys()
    print(f"  {len(existing)} already stored")

    taxyear = get_current_taxyear()
    print(f"Assessor tax year: {taxyear}")

    new_rows, events = [], []
    for year in SALE_YEARS:
        print(f"Downloading sales file for {year}...")
        sales = download_sales_csv(year)
        fresh = []
        for sale in sales:
            parcel = sale["Parcel"].strip()
            seq = (sale.get("SequenceNum") or "").strip()
            key = f"{parcel}_{seq}" if seq else f"{parcel}_{sale.get('RecordingDate','')}"
            if key not in existing:
                existing.add(key)  # guard against duplicates within the file
                fresh.append(sale)
        if SAMPLE_LIMIT:
            fresh = fresh[:SAMPLE_LIMIT]
        print(f"  {len(fresh)} new sales to process")

        for i, sale in enumerate(fresh, 1):
            detail = fetch_parcel_detail(sale["Parcel"].strip(), taxyear)
            row = build_row(sale, detail, now_iso)
            new_rows.append(row)
            events.append({
                "sale_key": row["sale_key"],
                "parcel": row["parcel"],
                "event_type": "new_sale",
                "owner_name": row.get("owner_name"),
                "property_address": row.get("property_address"),
                "sale_price": row.get("sale_price"),
                "has_solar": row.get("has_solar"),
                "occurred_at": now_iso,
            })
            if i % 100 == 0:
                print(f"  enriched {i}/{len(fresh)}")

    print(f"Total new sales: {len(new_rows)}")
    print("Writing to Supabase...")
    upsert("property_sales", new_rows, on_conflict="sale_key")
    upsert("property_sale_events", events)
    print("Done.")


if __name__ == "__main__":
    main()
