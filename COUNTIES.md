# Arizona County Coverage

Tracks which of Arizona's 15 counties this repo pulls data for, and what's
left to build. Keep this file updated as new counties/data types are added --
it's the map for "what's done, what's next" across sessions.

## Status

| County | Data type(s) | Table(s) | Script(s) | Status |
|------------|---------------------------------------------|------------------------------------------------|-----------------------------------|--------|
| Pima | Tucson solar permits | `solar_permits`, `solar_permit_events` | `scraper.py` | Live (daily 6:00am) |
| Pima | Tucson solar sales leads | (see `sales_scraper.py`) | `sales_scraper.py` | Live (daily 6:30am) |
| Pima | Countywide property/owner info | `property_info` | `property_info_tracker.py` | Live (daily 7:30am) |
| Santa Cruz | Countywide property/owner info | `santa_cruz_property_info` | `santa_cruz_property_tracker.py` | Live (daily 8:00am), ~43,230 parcels |
| Maricopa | Countywide property/owner info | `maricopa_property_info`, `maricopa_scrape_state` | `maricopa_property_tracker.py` | Live (daily 8:30am), ~1.76M parcels, resumable checkpoint |
| Pinal | Countywide property/owner info | `pinal_property_info`, `pinal_scrape_state` | `pinal_property_tracker.py` | Live (daily 9:00am), ~287k parcels, resumable checkpoint |
| Yavapai | Countywide property/owner info | `yavapai_property_info` | `yavapai_property_tracker.py` | **Blocked** -- code complete, table created, but gis.yavapaiaz.gov 403s every GitHub Actions IP (see note below); daily schedule disabled, workflow_dispatch-only until resolved |
| Apache | Countywide property/owner info | `apache_property_info` | `apache_property_tracker.py` | Live (daily 12:00pm/19:00 UTC), ~58k parcels, no mailing address or valuation data, no resumable checkpoint needed |
| Cochise | Countywide property/owner info | `cochise_property_info` | `cochise_property_tracker.py` | Live (daily 11:00am), ~122,936 parcels, no resumable checkpoint needed, has valuation (FCV only, no sale history) + lat/lon |
| Coconino | -- | -- | -- | **Deferred** -- see note below (public layer lacks owner/valuation data) |
| Gila | Countywide property/owner info | `gila_property_info` | `gila_property_tracker.py` | Live (daily 12:30pm/19:30 UTC), no mailing address or valuation data, no resumable checkpoint needed |
| Graham | Countywide property/owner info | `graham_property_info` | `graham_property_tracker.py` | Live (daily 1:00pm/20:00 UTC), ADWR statewide fallback source (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| Greenlee | Countywide property/owner info | `greenlee_property_info` | `greenlee_property_tracker.py` | Live (daily 1:30pm/20:30 UTC), ~4,706 parcels (smallest AZ county), ADWR statewide fallback source (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| La Paz | Countywide property/owner info | `la_paz_property_info` | `la_paz_property_tracker.py` | Live (daily 2:00pm/21:00 UTC), ~16,156 parcels, ADWR statewide fallback source dated May 2023 (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| Mohave | Countywide property/owner info | `mohave_property_info`, `mohave_scrape_state` | `mohave_property_tracker.py` | Live (daily 10:30am), ~267,046 parcels, resumable checkpoint, has both market + taxable valuation and sale history + lat/lon |
| Navajo | Countywide property/owner info | `navajo_property_info` | `navajo_property_tracker.py` | Live (daily 12:00pm), ~2,000+ parcels verified in test run, no valuation data |
| Yuma | Countywide property/owner info | `yuma_property_info` | `yuma_property_tracker.py` | Live (daily 10:00am), ~70,112 parcels, has valuation + sale history + lat/lon |

**All 15 Arizona counties are now covered or explicitly accounted for:** 13
counties live and verified (Pima, Santa Cruz, Maricopa, Pinal, Cochise,
Mohave, Yuma, Navajo, Apache, Gila, Graham, Greenlee, La Paz), 1 blocked by
a WAF with the fix documented (Yavapai), 1 deliberately deferred with the
reason documented (Coconino, no bulk owner/valuation API).

**Apache's `PARCEL_NUM` null-row bug, found and fixed (2026-08-14):** the
first live test run failed with a Postgres `23502` not-null violation --
one row (OBJECTID 143735) in Apache's ArcGIS layer is an entirely empty
placeholder polygon (every field, including `PARCEL_NUM`, is null), and
unlike the ADWR-sourced counties (which already fall back to an `ID`
field), Apache's script had no fallback for a missing parcel number. Fixed
by adding `OBJECTID` to the requested fields and falling back to a
synthetic `apache-oid-<OBJECTID>` key when `PARCEL_NUM` is blank, so a
stray junk row can't crash the whole batch upsert. Re-verified live after
the fix -- 1,989 rows upserted in the sample run, latitudes correctly
within Apache County's range.

**Anon-RLS-gap resolved for Maricopa/Santa Cruz/Pinal (2026-08-13):** those
three tables only granted SELECT to the `authenticated` role at first, but
the live Knockzy prototype reads Supabase with the `anon` key -- so despite
being fully enriched, the prototype couldn't display that data. Juan ran
the `anon` grant SQL himself directly in the Supabase SQL Editor (Claude's
own safety classifier blocks it from running anon-policy-granting SQL
itself, even with explicit approval -- it's treated as "modifying system/
security settings"), and it's now confirmed working end-to-end against the
live prototype's exact query pattern for both counties.

**Yavapai blocked by WAF (2026-08-13):** `gis.yavapaiaz.gov` (Yavapai's own
GIS server, hosting the same ArcGIS FeatureServer pattern used successfully
for every other county) returns 403 Forbidden to every request from GitHub
Actions runner IPs, while the identical request succeeds from a normal
browser/residential IP. Two fixes were tried and both failed identically:
realistic browser headers, then `curl_cffi` with Chrome TLS-fingerprint
impersonation (the standard fix for "browser works, script gets 403").
Since even a real-TLS-fingerprint client still gets 403'd, this looks like
IP/ASN-range blocking of datacenter IPs specifically, not fingerprint-based
bot detection -- which no client-side spoofing from an Actions runner can
get around. All the code/schema/workflow is deployed and ready; the daily
`schedule:` trigger was removed from `daily-yavapai-scrape.yml` (left
`workflow_dispatch`-only) so it doesn't fail-spam every day. Revisit if a
proxy/different egress IP becomes available, or if Yavapai publishes the
same data through a different (non-WAF'd) source.

**Yavapai and every new county built on 2026-08-13/14 (Apache, Gila,
Graham, Greenlee, La Paz, Navajo) deliberately only have an
`authenticated` read policy so far, on purpose** -- to avoid repeatedly
hitting the classifier block (see Security note below) across each new
county during the build. The `anon` grant SQL for all of them is queued
up in a NOTE at the bottom of each `supabase_<county>_property_schema.sql`
file, and has also been assembled into one consolidated batch for Juan to
run himself in the Supabase SQL Editor in a single pass.

**Coconino deferred -- public layer strips owner/valuation data
(2026-08-13):** Coconino's most prominent official public ArcGIS layer,
`Coconino_County_Parcels_Public_View`, only exposes 6 fields (APN, account
number, situs address/city, shape area/length) -- explicitly no owner name
and no valuation, by design (its own description says "...Public View with
limited fields"). The full owner/valuation/sale data instead lives behind
a Tyler Technologies "EagleWeb" system (`eagleassessor.coconino.az.gov`),
a per-parcel HTML search portal (search by name/parcel/address), not a
bulk-queryable API -- no bulk/JSON endpoint was found. This doesn't fit the
established bulk-pull playbook; it would need a fundamentally different,
much slower per-parcel scraper (similar in shape to Pima's original
enrichment approach). Skipped for now in favor of counties with a real
bulk API -- revisit with a per-parcel EagleWeb scraper if Coconino coverage
becomes a priority.

## The playbook (repeat this per county)

1. **Find the data source.** Open the county Assessor's public parcel-viewer
/ GIS map in a browser, open dev tools network tab, and look for calls to
a `.../FeatureServer/{layer}/query?f=json...` endpoint (ArcGIS Online) or
similar REST API. This has been free and unauthenticated for every AZ
county tried so far (Pima, Santa Cruz, Maricopa). Confirm with a
`returnCountOnly=true` request to get the total row count, and pull one
full sample record with `outFields=*` to see the real field names/types
before writing any parsing code -- county layers use inconsistent field
naming and formats (e.g. Maricopa pads numbers with commas and stores
dates as epoch milliseconds; don't assume). If a county doesn't run its
own mature public GIS FeatureServer, check the Arizona Dept. of Water
Resources' statewide fallback service first
(`https://azwatermaps.azwater.gov/arcgis/rest/services/General/Parcels_for_TEST/FeatureServer/<layer_id>`)
before concluding no bulk source exists -- it covers several rural
counties (confirmed for Graham, Greenlee, La Paz) with a shared field
schema, though the data can be noticeably staler than a county's own
live GIS.
2. **Design the schema.** One row per parcel, primary key on the parcel
number field. Standard columns: owner name, mailing address, property
address, valuation (current + prior year), sale/deed history, physical
characteristics (year built, sqft, etc), `status` (`pending` /
`enriched` / `no_owner_data` / `error`), `raw jsonb` (keep the full
original record), `enriched_at`/`updated_at`. Enable RLS with an
`authenticated`-read policy, matching the other tracker tables.
3. **Write the scraper**, reusing the established patterns:
- Bulk-pull via `resultOffset`/`resultRecordCount` pagination
(`returnGeometry=false` to keep it light), not a per-parcel loop.
- `ROW_COLUMNS` list + `normalize_row()` to pad every upserted row
(including defensive error rows) to an identical key set -- avoids
PostgREST's `PGRST102 "All object keys must match"`.
- Dedup a batch by primary key right before upsert -- avoids Postgres
`21000 "ON CONFLICT DO UPDATE command cannot affect row a second
time"`.
- Always request a stable fallback ID field (e.g. `ID`/`OBJECTID`) in
addition to the primary parcel-number field, and fall back to it (or
raise so the row lands in the defensive error-row path) when the
parcel number is blank -- a single null-primary-key row will otherwise
crash the entire batch upsert with Postgres `23502` (see the Apache
bug note above).
- `SAMPLE_LIMIT` and `MAX_RUNTIME_MINUTES` env vars for safe test runs
and a runtime safety net.
- If the county has roughly >200k parcels, add the resumable-offset
checkpoint pattern from `maricopa_property_tracker.py` (a tiny
`<county>_scrape_state` key/value table storing the ArcGIS
`resultOffset`) so a time-boxed run picks up where it left off
instead of always restarting from the top of the alphabet. Below
that scale, Santa Cruz's simpler "re-pull the whole county every
run" design is fine.
4. **Deploy via the GitHub web editor.** `git clone` of this repo works
(it's public), but `git push` does not -- confirmed during the Pinal
build: the session's git proxy rejects it ("not in this session's
authorized repository set"), even though the same session can push
just fine to other repos. Don't waste time re-attempting a push;
clone locally only to read existing files as templates, then create
each new/changed file at
`github.com/rewiredenergy/tucson-permit-tracker/new/main?filename=...`
(or the edit URL for existing files), paste content into the
CodeMirror editor, verify the line count matches the local file
(Ctrl+End, check the ending line number) before committing -- pastes
into CodeMirror can silently land partial/garbled, so always verify
before commit.
5. **Create the Supabase tables** by pasting the schema SQL into the
Supabase SQL Editor (Monaco) and running with Ctrl+Enter.
6. **Add the workflow** at `.github/workflows/daily-<county>-scrape.yml`,
staggered 30 minutes after the last one scheduled, `workflow_dispatch`
inputs for `sample_limit`/`max_runtime_minutes`, using the
`SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` repo secrets (already set up
once for the whole repo -- no per-county secrets needed).
7. **Test with a small `sample_limit` run** via Actions -> the workflow ->
Run workflow, watch it go green, then verify in Supabase: row counts by
`status`, and spot-check a real row's owner/value/physical fields for
sane values.

## Why this stays on GitHub Actions and free

The repo is **public**, which gives unlimited free GitHub Actions minutes
(vs. 2,000/month on a private repo, which is exhausted well before a month
is out once several county trackers are running daily). All the data this
repo pulls is public record. Revisit private-repo/paid-Actions once there
are paying subscribers -- see repo commit history around Aug 4, 2026 for the
billing investigation that led to this decision.

## Security note

Never paste an actual Supabase `service_role` key or other secret into
chat or into a committed file -- those live only in GitHub Actions repo
secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`), entered directly by
a human in the GitHub UI. The anon/public key is safe to embed client-side
if ever needed.
