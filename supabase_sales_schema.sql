-- ============================================================
-- Pima County New-Homeowner Tracker — Supabase schema
-- Run once in Supabase > SQL Editor (same as the permit schema).
-- ============================================================

-- One row per recorded property sale (from the Assessor's nightly
-- "Affidavits of Sales" file), enriched with the new owner's name,
-- the property address, and home characteristics.
create table public.property_sales (
  sale_key         text primary key,           -- parcel + '_' + affidavit sequence number
  jurisdiction     text not null default 'pima_county',
  parcel           text not null,              -- assessor parcel number (APN)
  sequence_num     text,                       -- recording sequence number

  sale_month       date,                       -- sale year+month from the affidavit
  recording_date   date,                       -- when the deed was recorded
  sale_price       numeric,
  property_type    text,                       -- e.g. "Single Family"
  intended_use     text,                       -- PrimaryRes / NonPrimary etc.
  deed_type        text,                       -- e.g. "Warranty Deed"
  financing        text,                       -- Cash / New Conventional / etc.
  validation_desc  text,                       -- assessor's sale-validity note
  buyer_seller_related text,
  has_solar        boolean,                    -- the affidavit asks if the home has solar!
  parcel_use       text,

  -- Enriched from the assessor's parcel API (the NEW owner):
  owner_name       text,
  mailing_address  text,
  property_address text,
  living_area_sqft numeric,
  year_built       int,
  stories          int,
  cooling          text,
  heating          text,

  raw              jsonb,                      -- full original sale row + parcel detail
  first_seen_at    timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index property_sales_parcel_idx    on public.property_sales (parcel);
create index property_sales_recorded_idx  on public.property_sales (recording_date desc);
create index property_sales_solar_idx     on public.property_sales (has_solar);
create index property_sales_seen_idx      on public.property_sales (first_seen_at desc);

-- Event log: one row per newly detected sale — powers a
-- "new homeowners" feed in Knockzy.
create table public.property_sale_events (
  id               bigint generated always as identity primary key,
  sale_key         text not null references public.property_sales (sale_key),
  parcel           text not null,
  event_type       text not null default 'new_sale',
  owner_name       text,
  property_address text,
  sale_price       numeric,
  has_solar        boolean,
  occurred_at      timestamptz not null default now()
);

create index property_sale_events_time_idx on public.property_sale_events (occurred_at desc);

-- Security: same pattern as solar_permits
alter table public.property_sales       enable row level security;
alter table public.property_sale_events enable row level security;

create policy "Authenticated users can read sales"
  on public.property_sales for select
  to authenticated
  using (true);

create policy "Authenticated users can read sale events"
  on public.property_sale_events for select
  to authenticated
  using (true);

-- OPTIONAL anon read (only if Knockzy reads without user login):
-- create policy "Anon can read sales"
--   on public.property_sales for select to anon using (true);
-- create policy "Anon can read sale events"
--   on public.property_sale_events for select to anon using (true);
