-- Gila County property/owner info table.
-- Source: services1.arcgis.com "ParcelService" FeatureServer (Gila County's
-- own layer, backs the county's public "Assessor Parcel Viewer" web app).
-- ~33,360 parcels as of 2026-08. See gila_property_tracker.py for details.
--
-- IMPORTANT GAP: this layer has NO valuation fields at all (no full cash
-- value, assessed value, land/improvement value) and no sale price/date
-- history. What's present: owner name(s), mailing address (split into
-- address/city/state/zip), situs address, land type, acreage (both acres
-- and sqft), and lat/lon via computed centroid.
-- Below the ~200k-parcel threshold, so no resumable checkpoint table is
-- needed (simple full re-pull every run, same as Santa Cruz/Yuma/Cochise/
-- Navajo/Apache).

create table if not exists gila_property_info (
    parcel text primary key,  -- APN (falls back to LAPN if APN is blank)
    jurisdiction text not null default 'gila_county',

    property_address text,  -- ADDRESS (situs)
    owner_name text,        -- Owner1
    owner_name_2 text,      -- Owner2
    account_number text,    -- ACCOUNTNO

    mailing_address_1 text,
    mailing_address_2 text,
    mailing_city text,
    mailing_state text,
    mailing_zip text,

    land_type text,
    land_size_acres double precision,
    land_size_sqft double precision,

    latitude double precision,
    longitude double precision,

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists gila_property_info_owner_name_idx
    on gila_property_info (owner_name);

create index if not exists gila_property_info_property_address_idx
    on gila_property_info (property_address);

create index if not exists gila_property_info_status_idx
    on gila_property_info (status);

alter table gila_property_info enable row level security;

create policy "Allow authenticated read access"
    on gila_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on gila_property_info
--     for select
--     to anon
--     using (true);
