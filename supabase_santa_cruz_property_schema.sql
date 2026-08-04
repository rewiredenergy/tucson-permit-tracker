-- Santa Cruz County Property Info schema
-- One row per Santa Cruz County, Arizona parcel. Unlike Pima County's
-- property_info table, this is populated entirely from two public bulk
-- ArcGIS FeatureServer layers published by the county's GIS/Assessor's
-- office (Parcels_Tile: owner name, mailing address, legal description,
-- assessed values; Buildings_Tile: building sqft, floor count, building
-- type/style), joined by parcel number (APN). Every run re-pulls and
-- re-upserts the full county in a handful of paginated bulk queries (no
-- per-parcel rate-limited API calls needed), so status is almost always
-- 'enriched' or 'no_owner_data' immediately after the first run -- there
-- is no long-running queue like Pima's property_info table.

create table if not exists santa_cruz_property_info (
  parcel                   text primary key,               -- APN, e.g. "115-08-292"
  jurisdiction              text not null default 'santa_cruz_county',

  -- identity / ownership (Parcels_Tile)
  property_address          text,                           -- SITEADDR
  owner_name                 text,                           -- OWNERNAME1 + OWNERNAME2
  mailing_address            text,                           -- MAIL + MAIL2 + MAIL3 + MAIL4
  legal_description          text,

  -- valuation (Parcels_Tile) -- county assessed value, not a market estimate
  land_value                 numeric,
  improvement_value          numeric,
  full_cash_value            numeric,
  full_cash_assessed         numeric,
  limited_value               numeric,
  limited_assessed            numeric,
  sale_price                  numeric,                       -- last recorded sale price, if any
  deed_type                   text,
  acreage                     numeric,
  tax_year                    text,

  -- physical characteristics (Buildings_Tile, joined by APN -- largest
  -- building on the parcel if there's more than one)
  interior_sqft                numeric,                      -- BLGAREA
  stories                       numeric,                      -- FLOORCOUNT
  property_type                 text,                         -- PROPCODE, e.g. "Residential"
  building_description          text,                         -- BUILDINGDESCRIPTION_1, e.g. "Ranch 1 Story"

  -- year built / room & bath counts -- left null: not available from the
  -- bulk GIS layers this tracker uses. See the script's module docstring
  -- for how a future per-parcel enrichment pass could add these.

  -- bookkeeping
  status                        text not null default 'pending',  -- pending | enriched | no_owner_data | error
  error_note                    text,
  raw                            jsonb,
  enriched_at                    timestamptz,
  updated_at                      timestamptz not null default now()
);

create index if not exists santa_cruz_property_info_status_idx on santa_cruz_property_info (status);
create index if not exists santa_cruz_property_info_owner_idx on santa_cruz_property_info (owner_name);

-- ------------------------------------------------------------
-- Security (Row Level Security) -- same pattern as the other
-- tracker tables. The scraper writes with the service-role key
-- (bypasses RLS). Knockzy users read through this policy.
-- ------------------------------------------------------------
alter table santa_cruz_property_info enable row level security;

create policy "Authenticated users can read santa cruz property info"
  on santa_cruz_property_info for select
  to authenticated
  using (true);

-- OPTIONAL: uncomment if parts of Knockzy read Supabase with the
-- anon key (no user login):
-- create policy "Anon can read santa cruz property info"
--   on santa_cruz_property_info for select to anon using (true);
