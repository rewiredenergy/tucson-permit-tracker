-- Coconino County property/owner info table.
-- Source: ADWR statewide "Parcels" MapServer, layer 2 ("Coconino") --
-- Arizona Department of Water Resources' broader "Parcel Finder" service,
-- which covers all 15 AZ counties as one layer each. ~78,368 parcels,
-- source extract dated November 2023. See coconino_property_tracker.py
-- for details, including a note on why a MapServer's /query endpoint is
-- used here (same behavior as the FeatureServer used for Graham/Greenlee/
-- La Paz/Yavapai).
--
-- WHY THIS COUNTY WAS PREVIOUSLY DEFERRED: Coconino County's own public
-- GIS layer strips owner name and valuation data entirely; real per-parcel
-- data lives behind Tyler Technologies' EagleWeb portal, which only
-- exposes one parcel at a time via HTML (no bulk API). This ADWR mirror
-- is a different, independently-maintained source that does include an
-- owner name field, unblocking bulk collection.
--
-- IMPORTANT GAP: this layer has NO mailing address field (only a situs
-- address, frequently blank) and NO valuation fields at all (no full cash
-- value, assessed value, land/improvement value) and no sale price/date
-- history. What's present: owner name, situs address pieces, book/map/
-- parcel/suffix, acreage, lat/lon. Data is noticeably staler than a county
-- running its own live GIS service -- this is the best/only public option
-- found for Coconino with owner-level data.
--
-- Below the ~200k-parcel threshold, so no resumable checkpoint table is
-- needed (simple full re-pull every run, same as Santa Cruz/Yuma/Cochise/
-- Navajo/Apache/Gila/Graham/Greenlee/La Paz/Yavapai).

create table if not exists coconino_property_info (
    parcel text primary key,  -- APN (falls back to the ADWR "ID" field if blank)
    jurisdiction text not null default 'coconino_county',

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

create index if not exists coconino_property_info_owner_name_idx
    on coconino_property_info (owner_name);

create index if not exists coconino_property_info_property_address_idx
    on coconino_property_info (property_address);

create index if not exists coconino_property_info_status_idx
    on coconino_property_info (status);

alter table coconino_property_info enable row level security;

create policy "Allow authenticated read access"
    on coconino_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on coconino_property_info
--     for select
--     to anon
--     using (true);
