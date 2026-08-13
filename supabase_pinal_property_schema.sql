-- Pinal County Property Info schema
-- One row per Pinal County, Arizona parcel (~287k parcels countywide --
-- larger than Santa Cruz's ~43k, smaller than Maricopa's ~1.76M).
-- Populated from a single public bulk ArcGIS FeatureServer layer
-- ("TaxParcels", the same backend behind the county's own public
-- Assessor Parcel Viewer at pinal.maps.arcgis.com) -- no API token
-- needed. A third-party mirror of a similarly-named layer exists on
-- Casa Grande's city GIS server but only carries ~60k parcels (a
-- partial/regional subset) -- gis.pinal.gov's own TaxParcels layer is
-- the authoritative full-county source. Like Santa Cruz and Maricopa,
-- there's no separate per-parcel enrichment step: owner name, mailing
-- address, year built, living area, and both current/prior valuation
-- are all already present on this one layer.
--
-- At ~287k parcels a full sweep is a lot of paginated requests even in
-- bulk mode, so progress is checkpointed in pinal_scrape_state so an
-- interrupted run resumes from where it left off next time, instead of
-- always restarting from the top of the alphabet. See
-- pinal_property_tracker.py.

create table if not exists pinal_property_info (
  parcel                     text primary key,               -- PARCELID
  jurisdiction                text not null default 'pinal_county',

  -- identity / ownership
  property_address             text,                           -- SITEADDRESS
  property_description          text,                           -- PRPRTYDSCRP (legal description)
  subdivision                     text,                           -- CNVYNAME
  owner_name                       text,                           -- OWNERNME1
  owner_name_2                      text,                           -- OWNERNME2
  mailing_address                    text,                           -- PSTLADDRESS
  mailing_city                        text,                           -- PSTLCITY
  mailing_state                        text,                           -- PSTLSTATE
  mailing_zip                           text,                           -- PSTLZIP5(-PSTLZIP4)

  -- classification -- CLASSDSCRP is the most useful single field for a
  -- residential/non-residential split downstream in Knockzy, e.g.
  -- "Owner Occupied Residential" / "Non-Primary Residence" /
  -- "Vacant Land / Non-Profit Imp" / "Residential Common Areas"
  property_class                         text,                           -- CLASSDSCRP
  property_use                            text,                           -- USEDSCRP

  -- physical characteristics
  year_built                               integer,                        -- RESYRBLT
  living_area_sqft                          numeric,                        -- RESFLRAREA
  structure_type                             text,                           -- RESSTRTYP
  floor_count                                 numeric,                        -- FLOORCOUNT

  -- valuation -- county assessed value, not a market estimate
  land_value                                   numeric,                        -- LNDVALUE
  assessed_value_current                        numeric,                        -- CNTASSDVAL
  assessed_value_previous                        numeric,                        -- PRVASSDVAL
  taxable_value_current                           numeric,                        -- CNTTXBLVAL
  taxable_value_previous                           numeric,                        -- PRVTXBLVAL

  -- sale history
  sale_price                                        numeric,                        -- SALEPRICE
  sale_date                                          date,                           -- SALEDATE (plain date string on this layer, not epoch ms)

  -- lot size
  land_size_acres                                     numeric,                        -- GROSSAC
  land_size_sqft                                       numeric,                        -- LANDSF

  source_last_updated                                   date,                           -- LASTUPDATE (epoch ms on this layer)

  -- bookkeeping
  status                                                  text not null default 'pending', -- pending | enriched | no_owner_data | error
  error_note                                               text,
  raw                                                        jsonb,
  enriched_at                                                timestamptz,
  updated_at                                                  timestamptz not null default now()
);

create index if not exists pinal_property_info_status_idx on pinal_property_info (status);
create index if not exists pinal_property_info_owner_idx on pinal_property_info (owner_name);
create index if not exists pinal_property_info_class_idx on pinal_property_info (property_class);

-- Tiny checkpoint table: tracks the ArcGIS resultOffset the last run left
-- off at, so a time-boxed or interrupted run resumes there next time
-- instead of restarting from offset 0 every day.
create table if not exists pinal_scrape_state (
  key         text primary key,
  value       text,
  updated_at  timestamptz not null default now()
);

-- ------------------------------------------------------------
-- Security (Row Level Security) -- same pattern as the other
-- tracker tables. The scraper writes with the service-role key
-- (bypasses RLS). Knockzy users read through this policy.
-- ------------------------------------------------------------
alter table pinal_property_info enable row level security;

create policy "Authenticated users can read pinal property info"
  on pinal_property_info for select
  to authenticated
  using (true);

-- NOTE: as of this table's creation, the live Knockzy prototype reads
-- Supabase with the ANON key (same as property_info/solar_permits,
-- which both also grant an "Anon can read ..." policy -- see those
-- tables for the pattern). maricopa_property_info and
-- santa_cruz_property_info were found to be MISSING that anon policy
-- at the same time this table was built, meaning the prototype
-- currently can't read either of those despite the data being fully
-- enriched. Broadening anon (unauthenticated) read access to
-- homeowner PII (names, addresses) across three tables is a real
-- privacy decision, not routine setup -- flagged to Juan rather than
-- applied automatically. Once a decision is made, the matching policy
-- for all three tables is:
--   create policy "Anon can read pinal property info"
--     on pinal_property_info for select to anon using (true);
