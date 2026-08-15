-- Yavapai County property/owner info table.
-- Source: ADWR statewide "Parcels_for_TEST" FeatureServer, layer 8
-- ("Yavapai_Parcels") -- the same public fallback service already used
-- for Graham/Greenlee/La Paz. ~186,484 parcels, source extract dated
-- May 2023. See yavapai_property_tracker.py for details.
--
-- REPLACES THE OLD SCHEMA (2026-08-13): the previous version of this table
-- was built for gis.yavapaiaz.gov, Yavapai County's own GIS FeatureServer,
-- which blocks every GitHub Actions runner IP with a WAF 403 -- confirmed
-- with two independent bypass attempts (realistic browser headers, then
-- curl_cffi Chrome-TLS impersonation). That table had 0 rows (the scraper
-- never successfully ran), so it's safe to fully replace with this
-- simpler, ADWR-sourced schema rather than migrate data. The old schema's
-- richer fields (parcel_label, subdivision, mailing address, care-of
-- address, zoning, account_number, source_last_updated) are dropped
-- because this replacement source doesn't provide them -- see the GAP
-- note below.
--
-- IMPORTANT GAP: this layer has NO mailing address field (only a situs
-- address, frequently blank) and NO valuation fields at all (no full cash
-- value, assessed value, land/improvement value) and no sale price/date
-- history. What's present: owner name, situs address pieces, book/map/
-- parcel/suffix, acreage, lat/lon. Data is noticeably staler than a county
-- running its own live GIS service -- this is the best/only public option
-- found for Yavapai now that the county's own service is permanently
-- unreachable from GitHub Actions.
--
-- Below the ~200k-parcel threshold, so no resumable checkpoint table is
-- needed (simple full re-pull every run, same as Santa Cruz/Yuma/Cochise/
-- Navajo/Apache/Gila/Graham/Greenlee/La Paz).

drop table if exists yavapai_property_info;

create table yavapai_property_info (
    parcel text primary key,  -- APN (falls back to the ADWR "ID" field if blank)
    jurisdiction text not null default 'yavapai_county',

    property_address text,  -- SITE_ADDRESS -- frequently blank
    property_city text,     -- SITE_CITY
    property_zip text,      -- SITE_ZIP
    owner_name text,        -- OWNER_NAME

    book text,           -- assessor parcel-number components
    map_number text,
    parcel_number text,
    suffix text,

    land_size_acres double precision,  -- ACRES_US

    latitude double precision,
    longitude double precision,

    source_url text,  -- link to the county assessor's own parcel search tool

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists yavapai_property_info_owner_name_idx
    on yavapai_property_info (owner_name);

create index if not exists yavapai_property_info_property_address_idx
    on yavapai_property_info (property_address);

create index if not exists yavapai_property_info_status_idx
    on yavapai_property_info (status);

alter table yavapai_property_info enable row level security;

create policy "Allow authenticated read access"
    on yavapai_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on yavapai_property_info
--     for select
--     to anon
--     using (true);
