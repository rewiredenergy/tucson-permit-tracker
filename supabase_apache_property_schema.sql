-- Apache County property/owner info table.
-- Source: services8.arcgis.com "Parcel8252025" FeatureServer (Apache County
-- GIS's own layer, wired into the county's public parcel web map). ~58,081
-- parcels as of 2026-08. See apache_property_tracker.py for details.
--
-- IMPORTANT GAP: unlike most other counties in this repo, this layer has
-- NO mailing address field and NO valuation fields at all (no full cash
-- value, assessed value, land/improvement value) and no sale price/date
-- history. What's present: owner name, situs address, acreage, lat/lon.
-- Below the ~200k-parcel threshold, so no resumable checkpoint table is
-- needed (simple full re-pull every run, same as Santa Cruz/Yuma/Cochise/
-- Navajo).

create table if not exists apache_property_info (
    parcel text primary key,  -- PARCEL_NUM, e.g. "101-73-039"
    jurisdiction text not null default 'apache_county',

    property_address text,  -- SITUS -- terse/inconsistent format, frequently blank
    owner_name text,
    account_number text,    -- NUMBER (Assessor account number)

    land_size_acres double precision,  -- SIZE field (stored as a string on the source, cast here)

    latitude double precision,
    longitude double precision,

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists apache_property_info_owner_name_idx
    on apache_property_info (owner_name);

create index if not exists apache_property_info_property_address_idx
    on apache_property_info (property_address);

create index if not exists apache_property_info_status_idx
    on apache_property_info (status);

alter table apache_property_info enable row level security;

create policy "Allow authenticated read access"
    on apache_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on apache_property_info
--     for select
--     to anon
--     using (true);
