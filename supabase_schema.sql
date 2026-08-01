-- ============================================================
-- Tucson Permit Tracker — Supabase schema
-- Paste this whole file into Supabase > SQL Editor > New query
-- and click RUN. Safe to run once. (Running twice will error
-- because the tables already exist — that's fine.)
-- ============================================================

-- Main table: one row per permit, kept up to date daily.
-- "trade_type" lets you add electrical / HVAC / roofing later
-- without changing the structure — just more rows.
create table public.solar_permits (
  permit_case_id   text primary key,          -- the portal's internal unique ID for the permit
  jurisdiction     text not null default 'tucson',
  trade_type       text not null default 'solar',

  permit_number    text not null,             -- e.g. TC-RES-1025-04820
  permit_type      text,                      -- e.g. "Residential Solar Permit"
  workclass        text,
  status           text,                      -- Issued / Withdrawn / Void / Expired / etc.
  project_name     text,
  description      text,                      -- e.g. "Installation of 5.81 kW PV System"

  address          text,                      -- full display address
  parcel_number    text,
  district         text,                      -- city ward, e.g. "Ward 2"

  square_feet      numeric,
  valuation        numeric,

  applicant_name   text,
  contractor_name  text,
  owner_name       text,

  applied_at       timestamptz,
  issued_at        timestamptz,
  expires_at       timestamptz,
  finaled_at       timestamptz,

  previous_status    text,                    -- what the status was before the last change
  status_changed_at  timestamptz,             -- when we noticed the change

  first_seen_at    timestamptz not null default now(),  -- when the scraper first saw this permit
  last_seen_at     timestamptz,                         -- last daily run that saw it on the portal
  updated_at       timestamptz not null default now(),

  raw              jsonb                      -- full original data from the portal, for safekeeping
);

create index solar_permits_trade_status_idx on public.solar_permits (trade_type, status);
create index solar_permits_applied_idx      on public.solar_permits (applied_at desc);
create index solar_permits_contractor_idx   on public.solar_permits (contractor_name);
create index solar_permits_first_seen_idx   on public.solar_permits (first_seen_at desc);

-- Event log: one row every time something happens — a brand-new
-- permit appears, or an existing permit changes status (including
-- withdrawals). This is what powers "what's new today" in Knockzy.
create table public.solar_permit_events (
  id              bigint generated always as identity primary key,
  permit_case_id  text not null references public.solar_permits (permit_case_id),
  permit_number   text not null,
  trade_type      text not null default 'solar',
  event_type      text not null,              -- 'new' or 'status_change'
  old_status      text,                       -- null for 'new'
  new_status      text,
  occurred_at     timestamptz not null default now()
);

create index solar_permit_events_time_idx on public.solar_permit_events (occurred_at desc);
create index solar_permit_events_type_idx on public.solar_permit_events (event_type);

-- ------------------------------------------------------------
-- Security (Row Level Security)
-- The scraper writes with your service-role key (bypasses RLS).
-- Knockzy users read through these policies.
-- ------------------------------------------------------------
alter table public.solar_permits       enable row level security;
alter table public.solar_permit_events enable row level security;

-- Logged-in Knockzy users can read. (Nobody can write except the
-- scraper's service-role key — no write policies are defined.)
create policy "Authenticated users can read permits"
  on public.solar_permits for select
  to authenticated
  using (true);

create policy "Authenticated users can read permit events"
  on public.solar_permit_events for select
  to authenticated
  using (true);

-- OPTIONAL: if parts of Knockzy read Supabase with the anon key
-- (no user login), uncomment these two policies as well:
-- create policy "Anon can read permits"
--   on public.solar_permits for select to anon using (true);
-- create policy "Anon can read permit events"
--   on public.solar_permit_events for select to anon using (true);
