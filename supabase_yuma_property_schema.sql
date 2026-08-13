-- Yuma County property/owner info table.
-- Source: gis.ci.yuma.az.us "pan/Parcels" MapServer, layer 0 ("County
-- Parcels") -- an automated weekly export of Yuma County Tax Assessor
-- records merged with the City of Yuma's parcel feature class. ~70,112
-- parcels as of 2026-08-13. See yuma_property_tracker.py for details.
--
-- Unlike Yavapai, this source has real valuation (LAND_FCV/IMPROVEMEN/
-- TOTAL_FCV/TOTAL_LPV) and sale history (SALEDOCNUM/SALE_DATE/
-- SALE_PRICE), plus lat/lon -- one of the richer sources in this repo.

create table if not exists yuma_property_info (
    parcel text primary key,
    jurisdiction text not null default 'yuma_county',

    parcel_label text,
    property_address text,
    legal_description text,
    subdivision text,
    block text,
    lot text,

    owner_name text,
    owner_name_2 text,
    business_name text,

    mailing_address text,
    mailing_city text,
    mailing_state text,
    mailing_zip text,

    land_size_acres double precision,
    parcel_sqft_gis double precision,  -- GIS-calculated parcel polygon area (land), NOT building sqft

    land_value double precision,
    improvement_value double precision,
    total_value double precision,      -- Full Cash Value (FCV)
    limited_value double precision,    -- Limited Property Value (LPV), used for AZ tax calc

    sale_document text,
    sale_date date,
    sale_price double precision,

    account_number text,
    tax_year text,
    property_code text,
    mobile_home_space text,

    latitude double precision,
    longitude double precision,

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists yuma_property_info_owner_name_idx
    on yuma_property_info (owner_name);

create index if not exists yuma_property_info_property_address_idx
    on yuma_property_info (property_address);

alter table yuma_property_info enable row level security;

create policy "Allow authenticated read access"
    on yuma_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on yuma_property_info
--     for select
--     to anon
--     using (true);
