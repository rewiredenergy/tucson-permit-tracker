-- Navajo County property/owner info table.
-- Source: services.arcgis.com "Parcels" FeatureServer (Navajo County GIS's
-- own official layer, owner account "NavajoCounty"). ~86,849 parcels as of
-- 2026-08-13. See navajo_property_tracker.py for details.
--
-- IMPORTANT GAP: unlike most other counties in this repo, this source has
-- NO valuation fields (no full cash value, assessed value, land/
-- improvement value) and no sale price/date history. What's present:
-- owner, mailing address, situs address, legal description, subdivision,
-- building count, account type, status, jurisdiction, zoning, acreage,
-- lat/lon. Below the ~200k-parcel threshold, so no resumable checkpoint
-- table is needed (simple full re-pull every run, same as Santa Cruz/
-- Yuma/Cochise).

create table if not exists navajo_property_info (
    parcel text primary key,  -- APN, e.g. "103-01-005A"
    jurisdiction text not null default 'navajo_county',

    account_number text,
    sheet text,
    trs text,  -- township/range/section
    legal_description text,

    property_address text,  -- SitusAddress

    owner_name text,

    mailing_address_1 text,
    mailing_address_2 text,
    mailing_address_raw text,  -- combined field as returned by the source, kept for reference

    subdivision text,
    building_count double precision,
    account_type text,      -- e.g. "Residential"
    parcel_status text,     -- source's own Status field (e.g. "Active"), NOT our scrape status
    zoning text,

    land_size_acres double precision,  -- Acreage field (already in acres)

    parcel_detail_url text,  -- per-parcel county web app link (valuation/tax history lives here, not in this bulk layer)

    latitude double precision,
    longitude double precision,

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists navajo_property_info_owner_name_idx
    on navajo_property_info (owner_name);

create index if not exists navajo_property_info_property_address_idx
    on navajo_property_info (property_address);

create index if not exists navajo_property_info_status_idx
    on navajo_property_info (status);

alter table navajo_property_info enable row level security;

create policy "Allow authenticated read access"
    on navajo_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on navajo_property_info
--     for select
--     to anon
--     using (true);
