-- Yavapai County Property Info schema
-- One row per Yavapai County, Arizona parcel (~188k parcels countywide --
-- between Santa Cruz's ~43k and Pinal's ~287k). Populated from a single
-- public bulk ArcGIS FeatureServer layer ("Parcels", layer 4 of the
-- "Property" service) published directly by Yavapai County's own GIS
-- server (gis.yavapaiaz.gov) -- no API token needed.
--
-- Unlike Pima/Santa Cruz/Maricopa/Pinal, this county's public parcel
-- layer has NO valuation, year-built, or square-footage data -- just
-- ownership, mailing address, situs (property) address, zoning,
-- subdivision, and deeded acreage. That's a real data-source gap, not
-- a scraper bug -- there's simply no free bulk source for Yavapai
-- valuation/building data at this time.
--
-- At ~188k parcels (under this repo's ~200k checkpoint threshold), a
-- full sweep completes in a few minutes -- no resumable-offset
-- checkpoint table needed, same simpler design as Santa Cruz's tracker.
-- See yavapai_property_tracker.py.

create table if not exists yavapai_property_info (
  parcel                text primary key,               -- PARCEL_ID
  jurisdiction          text not null default 'yavapai_county',

  -- identity / ownership
  parcel_label          text,                            -- PARLABEL (e.g. "201-09-001C")
  property_address      text,                            -- SITUS_ADD_DOR (physical property address)
  subdivision            text,                            -- SUBNAME
  owner_name              text,                            -- NAME
  owner_name_2             text,                            -- SECONDARY
  mailing_address           text,                            -- ADDRESS (owner's mailing address, can differ from property_address)
  mailing_city               text,                            -- CITY
  mailing_state               text,                            -- STATE
  mailing_zip                   text,                            -- ZIP (reformatted zip5-zip4)
  care_of_address                text,                            -- CO_ADDRESS

  -- physical / classification (no valuation or year-built available)
  land_size_acres                 numeric,                        -- ACRE_DEED
  zoning                            text,                            -- ZONING
  account_number                     text,                            -- ACCOUNTNO

  source_last_updated                  date,                           -- LASTUPDATED (epoch ms on this layer)

  -- bookkeeping
  status                                 text not null default 'pending', -- pending | enriched | no_owner_data | error
  error_note                              text,
  raw                                        jsonb,
  enriched_at                                 timestamptz,
  updated_at                                   timestamptz not null default now()
);

create index if not exists yavapai_property_info_status_idx on yavapai_property_info (status);
create index if not exists yavapai_property_info_owner_idx on yavapai_property_info (owner_name);
create index if not exists yavapai_property_info_zoning_idx on yavapai_property_info (zoning);

-- ------------------------------------------------------------
-- Security (Row Level Security) -- same pattern as the other
-- tracker tables. The scraper writes with the service-role key
-- (bypasses RLS). Knockzy users read through this policy.
-- ------------------------------------------------------------
alter table yavapai_property_info enable row level security;

create policy "Authenticated users can read yavapai property info"
  on yavapai_property_info for select
  to authenticated
  using (true);

-- NOTE: the live Knockzy prototype reads Supabase with the ANON key
-- (see the anon-RLS-gap note in COUNTIES.md, resolved 2026-08-13 for
-- maricopa/santa_cruz/pinal). Adding an anon policy is a privacy
-- decision that requires running SQL directly in the Supabase editor
-- yourself -- it's queued up in the consolidated anon-policy batch
-- delivered once all of today's new counties are built:
--   create policy "Anon can read yavapai property info"
--     on yavapai_property_info for select to anon using (true);
