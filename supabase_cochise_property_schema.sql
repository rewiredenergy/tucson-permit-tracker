-- Cochise County property/owner info table.
-- Source: services6.arcgis.com "Cad_Parcel_TaxInfo" FeatureServer (Cochise
-- County GIS's own official layer, "Parcel (Cochise County - Tax
-- Information)", updated weekly). ~122,936 parcels as of 2026-08-13. See
-- cochise_property_tracker.py for details.
--
-- Unlike Yuma/Mohave, this source does not include sale price/date
-- history -- just current owner/mailing info, valuation (FCV only),
-- acreage, and legal description. Below the ~200k-parcel threshold, so no
-- resumable checkpoint table is needed (simple full re-pull every run,
-- same as Santa Cruz/Yuma).

create table if not exists cochise_property_info (
    parcel text primary key,  -- apn, e.g. "10102001"
    jurisdiction text not null default 'cochise_county',

    reference text,
    tax_year text,
    account_type text,      -- accttype, e.g. "Residential"
    account_number text,
    tax_area_code text,

    property_address text,  -- situs_address

    owner_name text,
    owner_name_2 text,

    mailing_address_1 text,
    mailing_address_2 text,
    mailing_city text,
    mailing_state text,
    mailing_zip text,

    land_size_acres double precision,  -- acres field (already in acres)
    land_size_raw double precision,    -- parcel_size field, kept for reference

    use_code text,
    legal_description text,  -- legal_text

    full_cash_value double precision,  -- fcv
    ag_operator text,
    market_area text,
    market_subarea text,

    latitude double precision,   -- geo_y (decimal degrees, was returned as a string)
    longitude double precision,  -- geo_x (decimal degrees, was returned as a string)

    status text not null default 'pending',  -- pending / enriched / no_owner_data / error
    error_note text,
    raw jsonb,

    enriched_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists cochise_property_info_owner_name_idx
    on cochise_property_info (owner_name);

create index if not exists cochise_property_info_property_address_idx
    on cochise_property_info (property_address);

create index if not exists cochise_property_info_status_idx
    on cochise_property_info (status);

alter table cochise_property_info enable row level security;

create policy "Allow authenticated read access"
    on cochise_property_info
    for select
    to authenticated
    using (true);

-- NOTE: no `anon` read policy yet, on purpose -- see COUNTIES.md for why
-- this and other new-county anon grants are deferred to one consolidated
-- batch that Juan runs himself in the Supabase SQL Editor. Queued SQL:
--
-- create policy "Allow anon read access"
--     on cochise_property_info
--     for select
--     to anon
--     using (true);
