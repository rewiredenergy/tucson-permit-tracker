-- Mohave County property/owner info table.
-- Source: mcgis.mohave.gov "PARCELS" MapServer, layer 14 ("ParcelQueryLayer",
-- Mohave County Assessor's Office Tax PARCELS) -- ~267,046 parcels as of
-- 2026-08-13. See mohave_property_tracker.py for details.
--
-- Unusually rich for valuation: both the raw Full Cash Value / Limited
-- Property Value (full_cash_value / limited_value, pre-ratio) AND the
-- already-ratio-adjusted taxable amounts (assessed_full_cash_value /
-- assessed_limited_value) are present, plus real sale history and lat/lon.
--
-- Because a full sweep is a lot of paginated requests (~134 pages),
-- progress is checkpointed in mohave_scrape_state so an interrupted run
-- resumes from where it left off next time -- same pattern as Maricopa
-- and Pinal (both similarly over the ~200k-parcel threshold).

create table if not exists mohave_property_info (
    parcel text primary key,  -- TAXPIN, e.g. "210-38-047"
    jurisdiction text not null default 'mohave_county',

    parcel_type text,       -- TAXPARCELTYPE, e.g. "Base Parcel"
    exempt_status text,     -- EXEMPTSTATUS

    property_address text,  -- SITE_ADDRESS
    legal_description text,
    section_township_range text,  -- TWN_RNG_SEC

    owner_name text,
    owner_name_2 text,

    mailing_address text,
    mailing_city text,
    mailing_state text,
    mailing_zip text,

    use_code text,          -- USE_CODE
    property_type text,     -- PROPTYPE
    property_use text,      -- PROPUSE
    property_code text,     -- PROPCODE
    class_code text,        -- CLASS_CODE, e.g. "Vacant"

    land_size_acres double precision,  -- normalized from PARCEL_SIZE + UNIT_TYPE

    full_cash_value double precision,          -- FULL_CASH_VALUE (market, pre-ratio)
    limited_value double precision,            -- LIMITED_VALUE (LPV, pre-ratio)
    assessed_full_cash_value double precision, -- ASSESSED_FULL_CASH_VALUE (taxable, post-ratio)
    assessed_limited_value double precision,   -- ASSESSED_LIMITED (taxable, post-ratio)
    assessment_ratio double precision,         -- ASSESSMENT_RATIO
    land_value double precision,               -- LANDVALUE
    improvement_value double precision,        -- IMPVALUE

    sale_price double precision,
    sale_date date,
    deed_book text,
    deed_page text,
    deed_type text,
    receipt_number text,    -- RECPTNO

    account_number text,
    tax_year text,
    tax_area_code text,
    bos_district text,
    neighborhood_code text, -- NBHD

    latitude double precision,
    longitude double precision,
    altitude double precision,

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists mohave_property_info_owner_name_idx
    on mohave_property_info (owner_name);

create index if not exists mohave_property_info_property_address_idx
    on mohave_property_info (property_address);

create index if not exists mohave_property_info_status_idx
    on mohave_property_info (status);

-- Tiny checkpoint table: tracks the ArcGIS resultOffset the last run left
-- off at, so a time-boxed or interrupted run resumes there next time
-- instead of restarting from offset 0 every day.
create table if not exists mohave_scrape_state (
    key text primary key,
    value text,
    updated_at timestamptz not null default now()
);

alter table mohave_property_info enable row level security;

create policy "Allow authenticated read access"
    on mohave_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on mohave_property_info
--     for select
--     to anon
--     using (true);
