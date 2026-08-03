-- Property Info Tracker schema
-- One row per Pima County parcel. Seeded in bulk (status='pending') from the
-- county's public parcel-boundary GIS layer, then enriched parcel-by-parcel
-- from the Assessor's parcel-detail API, the county zoning GIS layer, and the
-- Treasurer's tax-inquiry page. A full-county sweep is expected to take many
-- scheduled runs to complete -- status tracks progress per parcel.

create table if not exists property_info (
  parcel                          text primary key,
  jurisdiction                    text not null default 'pima_county',

  -- identity / location (populated at seed time from the parcel roster)
  property_address                text,
  zip                              text,
  latitude                         double precision,
  longitude                        double precision,
  owner_name                       text,

  -- core physical characteristics (Assessor ResidentialChar)
  interior_sqft                   numeric,
  total_rooms                     integer,       -- ResidentialChar.ROOMS -- all rooms, NOT bedrooms
  bath_fixtures                   numeric,       -- ResidentialChar.BATHFIXTURES -- fixture count, NOT a literal bath count
  beds                            integer,       -- left null: not published by the county; no reliable free source found
  baths                           numeric,       -- left null: not published by the county; no reliable free source found
  lot_size_sqft                   numeric,
  stories                         numeric,
  roof_material                   text,
  wall_material                   text,
  year_built                      integer,
  property_type                   text,
  parcel_use_desc                 text,

  -- zoning (Pima County GIS "Zoning - All Jurisdictions" layer)
  zoning                          text,          -- e.g. "CR-5 PC" (base zone + jurisdiction suffix)
  zoning_base                     text,          -- e.g. "CR-5"
  zoning_jurisdiction             text,          -- e.g. "PIMA COUNTY" / "CITY OF TUCSON"

  -- HOA -- left null: no free public source identified (would require MLS/CC&R document search)
  hoa_yn                          boolean,
  hoa_name                        text,
  hoa_phone                       text,

  -- valuation (Assessor NoticedValuationData) -- county assessed value, not a market estimate
  assessed_full_cash_value        numeric,
  limited_assessed_value          numeric,
  price_per_sqft                  numeric,       -- computed: assessed_full_cash_value / interior_sqft

  -- tax (Treasurer property-inquiry page)
  annual_tax_amount               numeric,
  tax_year                        integer,

  -- electricity -- MODELED ESTIMATE, not a measured/scraped value. See basis text.
  projected_annual_electricity_bill numeric,
  electricity_estimate_basis      text,

  -- provenance / queue bookkeeping
  source_priority                 text,          -- 'sales_tracker' | 'permit_tracker' | 'county_sweep'
  status                          text not null default 'pending',  -- pending | enriched | not_residential | error
  error_note                      text,
  raw                             jsonb,
  enriched_at                     timestamptz,
  updated_at                      timestamptz not null default now()
);

create index if not exists property_info_status_idx on property_info (status);
create index if not exists property_info_source_priority_idx on property_info (source_priority);

-- ------------------------------------------------------------
-- Security (Row Level Security) -- same pattern as the other
-- tracker tables. The scraper writes with the service-role key
-- (bypasses RLS). Knockzy users read through this policy.
-- ------------------------------------------------------------
alter table property_info enable row level security;

create policy "Authenticated users can read property info"
  on property_info for select
  to authenticated
  using (true);

-- OPTIONAL: uncomment if parts of Knockzy read Supabase with the
-- anon key (no user login):
-- create policy "Anon can read property info"
--   on property_info for select to anon using (true);
