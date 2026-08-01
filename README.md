# Tucson Solar Permit Tracker

Pulls **every residential solar permit** from the City of Tucson's permit
portal once a day and stores them in your Supabase database, where Knockzy
can read them directly.

- Tracks both permit types: **Residential Solar Permit** (~6,745) and
  **Residential Solar App Permit / SolarAPP+** (~2,987)
- Captures: permit number, type, status, address, parcel, description
  (system size), contractor, applicant, owner, applied/issued/expiration/
  finalized dates, city ward, valuation
- Every day it records **what's new**, **what changed status** (including
  withdrawals) — not just a blind overwrite
- Built to expand: a `trade_type` column means electrical, HVAC, and
  roofing permits can be added later by adding a few lines of
  configuration, not rebuilding

## How it works (plain English)

The city's permit website is powered by a hidden data service. When you
click "Search" on the site, your browser quietly asks that service for
data. This scraper asks the same service directly — it's faster and far
more reliable than robot-clicking through web pages, and it needs no
login because this is public data.

Once a day, GitHub Actions (a free "run my script on a schedule" service
from GitHub) runs `scraper.py`, which:

1. Downloads the current list of all solar permits (100 at a time)
2. Compares against what's already in your Supabase table
3. For anything new or changed, fetches full details + contacts
4. Saves rows to the `solar_permits` table and logs each new permit or
   status change in `solar_permit_events`

## Setup (one time, ~15 minutes)

### Step 1 — Create the tables in Supabase
1. Go to your Supabase project dashboard → **SQL Editor** → **New query**
2. Paste the entire contents of `supabase_schema.sql` and click **Run**
3. You should see "Success. No rows returned" — the two tables now exist
   (check **Table Editor** in the left menu to see them)

### Step 2 — Create the GitHub repository
1. On github.com click **+** (top right) → **New repository**
2. Name it `tucson-permit-tracker`, set it to **Private**, click
   **Create repository**
3. Upload these files (**Add file → Upload files**, drag the whole
   folder contents in — including the `.github` folder*, then **Commit**):
   - `scraper.py`
   - `requirements.txt`
   - `supabase_schema.sql` (for reference)
   - `README.md`
   - `.github/workflows/daily-scrape.yml`

   *If drag-and-drop won't take the `.github` folder: create the file
   manually with **Add file → Create new file**, type
   `.github/workflows/daily-scrape.yml` as the name (the slashes create
   the folders), and paste the file's contents in.

### Step 3 — Add your two secrets (never in chat, never in code)
1. In the repo: **Settings → Secrets and variables → Actions →
   New repository secret**
2. Add secret #1 — Name: `SUPABASE_URL`
   Value: your project URL from Supabase → Project Settings → Data API
   (looks like `https://xxxxxxxx.supabase.co`)
3. Add secret #2 — Name: `SUPABASE_SERVICE_ROLE_KEY`
   Value: from Supabase → Project Settings → API Keys → `service_role`
   (click reveal, copy). **This key is powerful — it only ever lives in
   Supabase and GitHub Secrets, nowhere else.**

### Step 4 — Small test run first
1. Repo → **Actions** tab → enable workflows if prompted
2. Click **Daily Tucson permit scrape** → **Run workflow**
3. Type `25` in the sample_limit box → **Run workflow**
4. Wait ~2 minutes, click the run to watch the log. You should see
   something like `25 new, 0 changed` per type, then "Done."
5. Check Supabase → **Table Editor** → `solar_permits` — you should see
   ~50 rows of real permits

### Step 5 — Full first run (the big backfill)
1. Same as step 4 but leave sample_limit **blank**
2. First run fetches details for all ~9,700 permits politely
   (~3 requests/second), so it takes **roughly 2–3 hours**. Every run
   after that only fetches details for new/changed permits — usually
   **a few minutes**.

After that, it runs by itself every morning at 6:00 AM Arizona time.

## Checking it's working

- **GitHub → Actions tab**: green check = the run succeeded. GitHub can
  email you automatically if a run fails (Settings → Notifications).
- **Supabase → Table Editor → `solar_permits`**: the `last_seen_at`
  column should show today's date after each run.
- **Supabase → SQL Editor** quick health checks:

```sql
-- How many permits per status?
select status, count(*) from solar_permits group by status order by 2 desc;

-- What happened in the last 24 hours?
select event_type, permit_number, old_status, new_status, occurred_at
from solar_permit_events
where occurred_at > now() - interval '1 day'
order by occurred_at desc;

-- Top contractors by permit volume, last 90 days
select contractor_name, count(*)
from solar_permits
where applied_at > now() - interval '90 days'
group by 1 order by 2 desc limit 20;
```

## Hooking up Knockzy

Knockzy already talks to your Supabase project, so the data is
immediately readable — no new API needed. Example query from your
Next.js code:

```ts
// New permits and status changes from the last 7 days
const { data } = await supabase
  .from('solar_permit_events')
  .select('*')
  .gte('occurred_at', new Date(Date.now() - 7 * 864e5).toISOString())
  .order('occurred_at', { ascending: false });

// All active (issued, not expired) solar permits
const { data: active } = await supabase
  .from('solar_permits')
  .select('*')
  .eq('trade_type', 'solar')
  .eq('status', 'Issued')
  .order('issued_at', { ascending: false });
```

Note: the schema enables Row Level Security with read access for
logged-in users. If any Knockzy page reads Supabase *without* a user
being logged in, uncomment the two "anon" policies at the bottom of
`supabase_schema.sql` and run just those lines.

## Adding electrical / HVAC / roofing later

1. On the Tucson portal, run an Advanced Search for the permit type you
   want (e.g. "Residential Trade Permit")
2. Each permit type has an internal ID. Ask Claude (or check the
   dropdown's HTML) for the `PermitTypeId` value
3. Add an entry to `PERMIT_TYPES` at the top of `scraper.py`:

```python
{
    "trade_type": "electrical",   # or "hvac", "roofing"
    "label": "Residential Trade Permit",
    "permit_type_id": "the-long-id-here",
},
```

Everything else — table, diffing, events, schedule — already handles it.

## If the portal ever blocks GitHub's servers

The scraper talks to the portal like a normal web request. If Tyler
(the portal vendor) ever starts blocking automated traffic from GitHub's
computers, runs will start failing with connection errors. The fallback
is to run the same logic through a headless browser (Playwright), which
GitHub Actions supports — the code is structured so only the "fetch"
functions would need swapping. Ask Claude to make that change if it ever
becomes necessary.
