"""
Tucson Solar Permit Tracker
---------------------------
Pulls every residential solar permit from the City of Tucson's
EnerGov Citizen Self Service portal (via its JSON API — no browser
needed), then syncs them into a Supabase table (`solar_permits`).

Each run:
  1. Sweeps the full search results for every configured permit type.
  2. Compares against what's already in Supabase.
  3. For NEW or CHANGED permits, fetches the detail page + contacts
     (contractor / applicant / owner) from the portal.
  4. Upserts rows and writes an event log row for every new permit
     and every status change (including withdrawals).

Environment variables (set as GitHub Secrets — never hard-coded):
  SUPABASE_URL               e.g. https://abcdefgh.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the service_role secret from Supabase
  SAMPLE_LIMIT               optional; e.g. "25" for a small test run
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------
BASE = "https://cityoftucsonaz-energovweb.tylerhost.net/apps/selfservice/api"

# Headers the portal requires to identify the city ("tenant").
PORTAL_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "tenantId": "1",
    "tenantName": "cityoftucsonaz",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TucsonPermitTracker/1.0",
}

# Permit types to track. To add electrical / HVAC / roofing later,
# find their PermitTypeId (see README) and add entries here with the
# appropriate trade_type.
PERMIT_TYPES = [
    {
        "trade_type": "solar",
        "label": "Residential Solar Permit",
        "permit_type_id": "0c3d92bd-4adb-441c-a9f8-3e41902f3a08_2e1a2cd4-d386-4423-9180-416aa4f5599f",
    },
    {
        "trade_type": "solar",
        "label": "Residential Solar App Permit",
        "permit_type_id": "59226b5d-1b31-8973-b630-5810e4da65a8_6108ac78-89e8-8dd5-2211-3dce6373466e",
    },
]

PAGE_SIZE = 100          # search results per request
REQUEST_DELAY = 0.35     # seconds between portal requests (be polite)
MAX_RETRIES = 4

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT") or 0)  # 0 = no limit

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

session = requests.Session()


# ---------------------------------------------------------------
# The portal's search API needs this full request body, with every
# module's (unused) criteria present — captured from the live site.
# ---------------------------------------------------------------
def build_search_payload(permit_type_id: str, page_number: int, page_size: int) -> dict:
    return {
        "Keyword": "", "ExactMatch": True, "SearchModule": 2, "FilterModule": 1,
        "SearchMainAddress": False,
        "PlanCriteria": {
            "PlanNumber": None, "PlanTypeId": None, "PlanWorkclassId": None,
            "PlanStatusId": None, "ProjectName": None, "ApplyDateFrom": None,
            "ApplyDateTo": None, "ExpireDateFrom": None, "ExpireDateTo": None,
            "CompleteDateFrom": None, "CompleteDateTo": None, "Address": None,
            "Description": None, "SearchMainAddress": False, "ContactId": None,
            "ParcelNumber": None, "TypeId": None, "WorkClassIds": None,
            "ExcludeCases": None, "EnableDescriptionSearch": False,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "PermitCriteria": {
            "PermitNumber": None, "PermitTypeId": permit_type_id,
            "PermitWorkclassId": None, "PermitStatusId": "none", "ProjectName": None,
            "IssueDateFrom": None, "IssueDateTo": None, "Address": None,
            "Description": None, "ExpireDateFrom": None, "ExpireDateTo": None,
            "FinalDateFrom": None, "FinalDateTo": None, "ApplyDateFrom": None,
            "ApplyDateTo": None, "SearchMainAddress": False, "ContactId": None,
            "TypeId": None, "WorkClassIds": None, "ParcelNumber": None,
            "ExcludeCases": None, "EnableDescriptionSearch": False,
            "PageNumber": page_number, "PageSize": page_size,
            "SortBy": "PermitNumber.keyword", "SortAscending": True,
        },
        "InspectionCriteria": {
            "Keyword": None, "ExactMatch": False, "Complete": None,
            "InspectionNumber": None, "InspectionTypeId": None,
            "InspectionStatusId": None, "RequestDateFrom": None,
            "RequestDateTo": None, "ScheduleDateFrom": None, "ScheduleDateTo": None,
            "Address": None, "SearchMainAddress": False, "ContactId": None,
            "TypeId": [], "WorkClassIds": [], "ParcelNumber": None,
            "DisplayCodeInspections": False, "ExcludeCases": [],
            "ExcludeFilterModules": [], "HiddenInspectionTypeIDs": None,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "CodeCaseCriteria": {
            "CodeCaseNumber": None, "CodeCaseTypeId": None, "CodeCaseStatusId": None,
            "ProjectName": None, "OpenedDateFrom": None, "OpenedDateTo": None,
            "ClosedDateFrom": None, "ClosedDateTo": None, "Address": None,
            "ParcelNumber": None, "Description": None, "SearchMainAddress": False,
            "RequestId": None, "ExcludeCases": None, "ContactId": None,
            "EnableDescriptionSearch": False, "HiddenCodeCaseTypeIds": None,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "RequestCriteria": {
            "RequestNumber": None, "RequestTypeId": None, "RequestStatusId": None,
            "ProjectName": None, "EnteredDateFrom": None, "EnteredDateTo": None,
            "DeadlineDateFrom": None, "DeadlineDateTo": None, "CompleteDateFrom": None,
            "CompleteDateTo": None, "Address": None, "ParcelNumber": None,
            "SearchMainAddress": False,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "BusinessLicenseCriteria": {
            "LicenseNumber": None, "LicenseTypeId": None, "LicenseClassId": None,
            "LicenseStatusId": None, "BusinessStatusId": None, "LicenseYear": None,
            "ApplicationDateFrom": None, "ApplicationDateTo": None,
            "IssueDateFrom": None, "IssueDateTo": None, "ExpirationDateFrom": None,
            "ExpirationDateTo": None, "SearchMainAddress": False,
            "CompanyTypeId": None, "CompanyName": None, "BusinessTypeId": None,
            "Description": None, "CompanyOpenedDateFrom": None,
            "CompanyOpenedDateTo": None, "CompanyClosedDateFrom": None,
            "CompanyClosedDateTo": None, "LastAuditDateFrom": None,
            "LastAuditDateTo": None, "ParcelNumber": None, "Address": None,
            "TaxID": None, "DBA": None, "ExcludeCases": None, "TypeId": None,
            "WorkClassIds": None, "ContactId": None,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "ProfessionalLicenseCriteria": {
            "LicenseNumber": None, "HolderFirstName": None, "HolderMiddleName": None,
            "HolderLastName": None, "HolderCompanyName": None, "LicenseTypeId": None,
            "LicenseClassId": None, "LicenseStatusId": None, "IssueDateFrom": None,
            "IssueDateTo": None, "ExpirationDateFrom": None, "ExpirationDateTo": None,
            "ApplicationDateFrom": None, "ApplicationDateTo": None, "Address": None,
            "MainParcel": None, "SearchMainAddress": False, "ExcludeCases": None,
            "TypeId": None, "WorkClassIds": None, "ContactId": None,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "LicenseCriteria": {
            "LicenseNumber": None, "LicenseTypeId": None, "LicenseClassId": None,
            "LicenseStatusId": None, "BusinessStatusId": None,
            "ApplicationDateFrom": None, "ApplicationDateTo": None,
            "IssueDateFrom": None, "IssueDateTo": None, "ExpirationDateFrom": None,
            "ExpirationDateTo": None, "SearchMainAddress": False,
            "CompanyTypeId": None, "CompanyName": None, "BusinessTypeId": None,
            "Description": None, "CompanyOpenedDateFrom": None,
            "CompanyOpenedDateTo": None, "CompanyClosedDateFrom": None,
            "CompanyClosedDateTo": None, "LastAuditDateFrom": None,
            "LastAuditDateTo": None, "ParcelNumber": None, "Address": None,
            "TaxID": None, "DBA": None, "ExcludeCases": None, "TypeId": None,
            "WorkClassIds": None, "ContactId": None, "HolderFirstName": None,
            "HolderMiddleName": None, "HolderLastName": None, "MainParcel": None,
            "EnableDescriptionSearchForBLicense": False,
            "EnableDescriptionSearchForPLicense": False,
            "EnableDescriptionSearchForOperationalPermit": False,
            "IsOperationalPermit": False,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "ProjectCriteria": {
            "ProjectNumber": None, "ProjectName": None, "Address": None,
            "ParcelNumber": None, "StartDateFrom": None, "StartDateTo": None,
            "ExpectedEndDateFrom": None, "ExpectedEndDateTo": None,
            "CompleteDateFrom": None, "CompleteDateTo": None, "Description": None,
            "SearchMainAddress": False, "ContactId": None, "TypeId": None,
            "ExcludeCases": None, "EnableDescriptionSearch": False,
            "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
        },
        "PlanSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "PlanNumber.keyword", "Value": "Plan Number"},
            {"Key": "ProjectName.keyword", "Value": "Project"},
            {"Key": "MainAddress", "Value": "Address"},
            {"Key": "ApplyDate", "Value": "Apply Date"},
        ],
        "PermitSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "PermitNumber.keyword", "Value": "Permit Number"},
            {"Key": "ProjectName.keyword", "Value": "Project"},
            {"Key": "MainAddress", "Value": "Address"},
            {"Key": "IssueDate", "Value": "Issued Date"},
            {"Key": "FinalDate", "Value": "Finalized Date"},
        ],
        "InspectionSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "InspectionNumber.keyword", "Value": "Inspection Number"},
            {"Key": "MainAddress", "Value": "Address"},
            {"Key": "ScheduledDate", "Value": "Schedule Date"},
            {"Key": "RequestDate", "Value": "Request Date"},
        ],
        "CodeCaseSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "CaseNumber.keyword", "Value": "Code Case Number"},
            {"Key": "ProjectName.keyword", "Value": "Project"},
            {"Key": "MainAddress", "Value": "Address"},
            {"Key": "OpenedDate", "Value": "Opened Date"},
            {"Key": "ClosedDate", "Value": "Closed Date"},
        ],
        "RequestSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "RequestNumber.keyword", "Value": "Request Number"},
            {"Key": "ProjectName.keyword", "Value": "Project Name"},
            {"Key": "MainAddress", "Value": "Address"},
            {"Key": "EnteredDate", "Value": "Date Entered"},
            {"Key": "CompleteDate", "Value": "Completion Date"},
        ],
        "LicenseSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "LicenseNumber.keyword", "Value": "License Number"},
            {"Key": "LicenseNumber.keyword", "Value": "Operational Permit Number"},
            {"Key": "CompanyName.keyword", "Value": "Company Name"},
            {"Key": "AppliedDate", "Value": "Applied Date"},
            {"Key": "MainAddress", "Value": "Address"},
        ],
        "ProjectSortList": [
            {"Key": "relevance", "Value": "Relevance"},
            {"Key": "ProjectNumber.keyword", "Value": "Project Number"},
            {"Key": "ProjectName.keyword", "Value": "Project Name"},
            {"Key": "StartDate", "Value": "Start Date"},
            {"Key": "CompleteDate", "Value": "Completed Date"},
            {"Key": "ExpectedEndDate", "Value": "Expected End Date"},
            {"Key": "MainAddress", "Value": "Address"},
        ],
        "ExcludeCases": None,
        "SortOrderList": [
            {"Key": True, "Value": "Ascending"},
            {"Key": False, "Value": "Descending"},
        ],
        "HiddenInspectionTypeIDs": None,
        "PageNumber": 0, "PageSize": 0, "SortBy": "relevance", "SortAscending": True,
    }


# ---------------------------------------------------------------
# Portal API helpers
# ---------------------------------------------------------------
def portal_post(path: str, payload: dict, max_retries: int = MAX_RETRIES) -> dict:
    """POST to the portal with retries and polite delay."""
    url = f"{BASE}{path}"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(REQUEST_DELAY)
            resp = session.post(url, headers=PORTAL_HEADERS,
                                data=json.dumps(payload), timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                # EnerGov wraps errors in a 200 with Success=false
                if isinstance(data, dict) and data.get("Success") is False:
                    raise RuntimeError(f"Portal error: {data.get('ErrorMessage')}")
                return data
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * attempt
            print(f"  retry {attempt}/{max_retries} for {path} after error: {e} "
                  f"(waiting {wait}s)")
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {path}: {last_err}")


def search_permits(type_cfg: dict) -> list[dict]:
    """Page through the search results for one permit type."""
    records = []
    page = 1
    while True:
        payload = build_search_payload(type_cfg["permit_type_id"], page, PAGE_SIZE)
        data = portal_post("/energov/search/search", payload)
        result = data.get("Result") or {}
        batch = result.get("EntityResults") or []
        total_pages = result.get("TotalPages") or 0
        if page == 1:
            print(f"  {type_cfg['label']}: {result.get('TotalFound')} permits, "
                  f"{total_pages} pages")
        records.extend(batch)
        if SAMPLE_LIMIT and len(records) >= SAMPLE_LIMIT:
            records = records[:SAMPLE_LIMIT]
            print(f"  SAMPLE_LIMIT={SAMPLE_LIMIT} reached — stopping early (test mode)")
            break
        if page >= total_pages or not batch:
            break
        page += 1

    # Safety: de-duplicate by CaseId in case page boundaries shifted
    # between requests (a permit could otherwise appear twice).
    seen: set[str] = set()
    unique = []
    for r in records:
        cid = r.get("CaseId")
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(r)
    return unique


def fetch_detail(case_id: str) -> dict:
    # Some legacy permits always error on these endpoints — don't burn
    # time on long retries for them (max_retries=2).
    data = portal_post("/energov/permits/permitdetail",
                       {"EntityId": case_id, "ModuleId": 1}, max_retries=2)
    return data.get("Result") or {}


def fetch_contacts(case_id: str) -> list[dict]:
    data = portal_post("/energov/entity/contacts/search/search", {
        "PageNumber": 1, "PageSize": 25, "SortField": "",
        "IsSortedInAscendingOrder": True, "ModuleId": 1, "EntityId": case_id,
    }, max_retries=2)
    return data.get("Result") or []


# ---------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------
def to_phoenix_ts(value: str | None) -> str | None:
    """Portal timestamps are local Arizona time with no zone marker.
    Arizona is always UTC-7 (no daylight saving)."""
    if not value:
        return None
    return f"{value}-07:00"


def contact_name(contact: dict) -> str:
    company = (contact.get("GlobalEntityName") or "").strip()
    person = " ".join(p for p in [contact.get("FirstName"), contact.get("LastName")]
                      if p).strip()
    return company or person


def build_row(rec: dict, detail: dict | None, contacts: list[dict] | None,
              type_cfg: dict, now_iso: str) -> dict:
    """Full row for a new/changed permit (includes detail + contacts).

    Every key is always present (None when a lookup failed) because
    Supabase bulk writes require identical keys on every row.
    """
    row = build_light_row(rec, type_cfg, now_iso)
    row.update({
        "description": None, "district": None, "square_feet": None,
        "valuation": None, "applicant_name": None, "contractor_name": None,
        "owner_name": None, "previous_status": None, "status_changed_at": None,
    })
    if detail is not None:
        row["description"] = detail.get("Description")
        row["district"] = detail.get("DistrictName")
        row["square_feet"] = detail.get("SquareFeet")
        row["valuation"] = detail.get("Value")
    if contacts is not None:
        by_type: dict[str, str] = {}
        for c in contacts:
            ctype = (c.get("ContactTypeName") or "").strip().lower()
            name = contact_name(c)
            if name and ctype not in by_type:
                by_type[ctype] = name
        row["applicant_name"] = by_type.get("applicant")
        row["contractor_name"] = by_type.get("contractor")
        row["owner_name"] = by_type.get("owner")
    row["raw"] = {"search": rec, "detail": detail, "contacts": contacts}
    return row


def build_light_row(rec: dict, type_cfg: dict, now_iso: str) -> dict:
    """Search-level fields only — used to refresh unchanged permits."""
    return {
        "permit_case_id": rec["CaseId"],
        "jurisdiction": "tucson",
        "trade_type": type_cfg["trade_type"],
        "permit_number": rec.get("CaseNumber"),
        "permit_type": rec.get("CaseType"),
        "workclass": rec.get("CaseWorkclass"),
        "status": rec.get("CaseStatus"),
        "project_name": rec.get("ProjectName") or None,
        "address": rec.get("AddressDisplay"),
        "parcel_number": rec.get("MainParcel"),
        "applied_at": to_phoenix_ts(rec.get("ApplyDate")),
        "issued_at": to_phoenix_ts(rec.get("IssueDate")),
        "expires_at": to_phoenix_ts(rec.get("ExpireDate")),
        "finaled_at": to_phoenix_ts(rec.get("FinalDate")),
        "last_seen_at": now_iso,
        "updated_at": now_iso,
    }


# ---------------------------------------------------------------
# Supabase helpers (REST API)
# ---------------------------------------------------------------
def supabase_get_existing() -> dict[str, str]:
    """Return {permit_case_id: status} for everything already stored."""
    existing: dict[str, str] = {}
    offset, chunk = 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/solar_permits"
               f"?select=permit_case_id,status&limit={chunk}&offset={offset}")
        resp = session.get(url, headers=SUPABASE_HEADERS, timeout=60)
        resp.raise_for_status()
        rows = resp.json()
        for r in rows:
            existing[r["permit_case_id"]] = r["status"]
        if len(rows) < chunk:
            return existing
        offset += chunk


def supabase_upsert(rows: list[dict]) -> None:
    if not rows:
        return
    url = (f"{SUPABASE_URL}/rest/v1/solar_permits"
           f"?on_conflict=permit_case_id")
    headers = {**SUPABASE_HEADERS,
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        resp = session.post(url, headers=headers, data=json.dumps(batch), timeout=120)
        if resp.status_code >= 300:
            raise RuntimeError(f"Supabase upsert failed "
                               f"({resp.status_code}): {resp.text[:300]}")


def supabase_insert_events(events: list[dict]) -> None:
    if not events:
        return
    url = f"{SUPABASE_URL}/rest/v1/solar_permit_events"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    for i in range(0, len(events), 500):
        batch = events[i:i + 500]
        resp = session.post(url, headers=headers, data=json.dumps(batch), timeout=120)
        if resp.status_code >= 300:
            raise RuntimeError(f"Supabase event insert failed "
                               f"({resp.status_code}): {resp.text[:300]}")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"Run started {now_iso}"
          + (f" — TEST MODE, sample limit {SAMPLE_LIMIT}" if SAMPLE_LIMIT else ""))

    print("Loading existing permits from Supabase...")
    existing = supabase_get_existing()
    print(f"  {len(existing)} already stored")

    all_full_rows: list[dict] = []
    all_light_rows: list[dict] = []
    events: list[dict] = []
    stats = {"new": 0, "changed": 0, "unchanged": 0}

    def flush() -> None:
        """Write buffered rows to Supabase and clear the buffers.
        Called periodically so a late failure never loses hours of work."""
        supabase_upsert(all_full_rows)   # permits before events
        supabase_insert_events(events)   # (events reference permits by ID)
        if all_full_rows or events:
            print(f"  saved batch: {len(all_full_rows)} permits, "
                  f"{len(events)} events")
        all_full_rows.clear()
        events.clear()

    for type_cfg in PERMIT_TYPES:
        print(f"Searching: {type_cfg['label']}...")
        records = search_permits(type_cfg)

        for rec in records:
            case_id = rec.get("CaseId")
            status = rec.get("CaseStatus")
            if not case_id:
                continue

            if case_id not in existing:
                kind = "new"
            elif existing[case_id] != status:
                kind = "changed"
            else:
                kind = "unchanged"
            stats[kind] += 1

            if kind == "unchanged":
                all_light_rows.append(build_light_row(rec, type_cfg, now_iso))
                continue

            # New or changed: also grab detail + contacts
            detail = contacts = None
            try:
                detail = fetch_detail(case_id)
                contacts = fetch_contacts(case_id)
            except Exception as e:  # noqa: BLE001
                print(f"  warning: detail/contacts failed for "
                      f"{rec.get('CaseNumber')}: {e}")
            row = build_row(rec, detail, contacts, type_cfg, now_iso)
            if kind == "changed":
                row["previous_status"] = existing[case_id]
                row["status_changed_at"] = now_iso
            all_full_rows.append(row)

            events.append({
                "permit_case_id": case_id,
                "permit_number": rec.get("CaseNumber"),
                "trade_type": type_cfg["trade_type"],
                "event_type": "new" if kind == "new" else "status_change",
                "old_status": existing.get(case_id),
                "new_status": status,
                "occurred_at": now_iso,
            })
            if len(all_full_rows) >= 250:
                flush()

    print(f"Results: {stats['new']} new, {stats['changed']} changed, "
          f"{stats['unchanged']} unchanged")

    print("Writing final batches to Supabase...")
    flush()
    supabase_upsert(all_light_rows)
    print("Done.")


if __name__ == "__main__":
    main()
