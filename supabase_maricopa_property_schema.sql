-- Maricopa County Property Info schema
-- One row per Maricopa County, Arizona parcel (~1.76M parcels countywide --
-- the largest county by far in this repo, ~40x Santa Cruz and ~4x Pima).
-- Populated from a single public bulk ArcGIS FeatureServer layer
-- ("Parcels_view", the same backend behind the Assessor's own public
-- Parcel Viewer at maps.mcassessor.maricopa.gov) -- no paid Data Sales
-- download and no API token needed. Unlike Pima's property_info table,
-- there's no separate per-parcel enrichment step: owner name, mailing
-- address, year built, living space, and valuation are all already
-- present on this one layer.
--
-- Because a full sweep is a lot of paginated requests even in bulk mode,
-- progress is checkpointed in maricopa_scrape_state so an interrupted
-- run resumes from where it left off next time, instead of always
-- restarting from the top of the alphabet and starving later parcels of
-- updates. See maricopa_property_tracker.py.

create table if not exists maricopa_property_info (
  parcel                     text primary key,               -- APN_DASH, e.g. "132-75-013"
  jurisdiction                text not null default 'maricopa_county',
  municipality                 text,                           -- JURISDICTION field, e.g. "TEMPE" / "PHOENIX"

  -- identity / ownership
  property_address             text,                           -- PHYSICAL_ADDRESS
  owner_name                    text,                           -- OWNER_NAME
  mailing_address                text,                           -- MAIL_ADDRESS
  care_of                         text,                           -- INCAREOF
  subdivision                     text,                           -- SUBNAME
  lot_number                       text,                           -- LOT_NUM
  section_township_range            text,                           -- STR

  -- valuation -- county assessed value, not a market estimate
  full_cash_value_current            numeric,                        -- FCV_CUR
  limited_value_current               numeric,                        -- LPV_CUR
  legal_class_current                  text,                           -- LC_CUR
  tax_year_current                      text,                           -- TAX_YR_CUR
  full_cash_value_previous               numeric,                        -- FCV_PREV
  limited_value_previous                  numeric,                        -- LPV_PREV
  legal_class_previous                     text,                           -- LC_PREV
  tax_year_previous                         text,                           -- TAX_YR_PREV

  -- sale / deed history
  sale_price                                 numeric,
  sale_date                                   date,
  deed_number                                  text,
  deed_date                                     date,

  -- physical characteristics
  construction_year                              integer,                        -- CONST_YEAR
  living_space_sqft                               numeric,                        -- LIVING_SPACE
  land_size_sqft                                   numeric,                        -- LAND_SIZE
  floor_count                                       numeric,                        -- FLOOR
  property_use_code                                  text,                           -- PUC
  zoning                                              text,                           -- CITY_ZONING

  latitude                                             double precision,
  longitude                                             double precision,

  -- bookkeeping
  status                                                 text not null default 'pending', -- pending | enriched | no_owner_data | error
  error_note                                              text,
  raw                                                       jsonb,
  enriched_at                                               timestamptz,
  updated_at                                                 timestamptz not null default now()
);

create index if not exists maricopa_property_info_status_idx on maricopa_property_info (status);
create index if not exists maricopa_property_info_owner_idx on maricopa_property_info (owner_name);
create index if not exists maricopa_property_info_municipality_idx on maricopa_property_info (municipality);

-- Tiny checkpoint table: tracks the ArcGIS resultOffset the last run left
-- off at, so a time-boxed or interrupted run resumes there next time
-- instead of restarting from offset 0 every day.
create table if not exists maricopa_scrape_state (
  key         text primary key,
  value       text,
  updated_at  timestamptz not null default now()
);

-- ------------------------------------------------------------
-- Security (Row Level Security) -- same pattern as the other
-- tracker tables. The scraper writes with the service-role key
-- (bypasses RLS). Knockzy users read through this policy.
-- ------------------------------------------------------------
alter table maricopa_property_info enable row level security;

create policy "Authenticated users can read maricopa property info"
  on maricopa_property_info for select
  to authenticated
  using (true);

-- OPTIONAL: uncomment if parts of Knockzy read Supabase with the
-- anon key (no user login):
-- create policy "Anon can read maricopa property info"
--   on maricopa_property_info for select to anon using (true);
