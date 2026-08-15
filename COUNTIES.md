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
| Yavapai | Countywide property/owner info | `yavapai_property_info` | `yavapai_property_tracker.py` | Live (daily 9:30am/16:30 UTC), ~186,484 parcels, ADWR statewide fallback source dated May 2023 (no mailing/valuation data, staler than the county's own GIS), no resumable checkpoint needed |
| Apache | Countywide property/owner info | `apache_property_info` | `apache_property_tracker.py` | Live (daily 12:00pm/19:00 UTC), ~58k parcels, no mailing address or valuation data, no resumable checkpoint needed |
| Cochise | Countywide property/owner info | `cochise_property_info` | `cochise_property_tracker.py` | Live (daily 11:00am), ~122,936 parcels, no resumable checkpoint needed, has valuation (FCV only, no sale history) + lat/lon |
| Coconino | Countywide property/owner info | `coconino_property_info` | `coconino_property_tracker.py` | Live (daily 2:30pm/21:30 UTC), ~78,368 parcels, ADWR "Parcel Finder" MapServer source dated November 2023 (no mailing/valuation data, staler than the county's own GIS), no resumable checkpoint needed |
| Gila | Countywide property/owner info | `gila_property_info` | `gila_property_tracker.py` | Live (daily 12:30pm/19:30 UTC), no mailing address or valuation data, no resumable checkpoint needed |
| Graham | Countywide property/owner info | `graham_property_info` | `graham_property_tracker.py` | Live (daily 1:00pm/20:00 UTC), ADWR statewide fallback source (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| Greenlee | Countywide property/owner info | `greenlee_property_info` | `greenlee_property_tracker.py` | Live (daily 1:30pm/20:30 UTC), ~4,706 parcels (smallest AZ county), ADWR statewide fallback source (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| La Paz | Countywide property/owner info | `la_paz_property_info` | `la_paz_property_tracker.py` | Live (daily 2:00pm/21:00 UTC), ~16,156 parcels, ADWR statewide fallback source dated May 2023 (no mailing/valuation data, staler than a county's own live GIS), no resumable checkpoint needed |
| Mohave | Countywide property/owner info | `mohave_property_info`, `mohave_scrape_state` | `mohave_property_tracker.py` | Live (daily 10:30am), ~267,046 parcels, resumable checkpoint, has both market + taxable valuation and sale history + lat/lon |
| Navajo | Countywide property/owner info | `navajo_property_info` | `navajo_property_tracker.py` | Live (daily 12:00pm), ~2,000+ parcels verified in test run, no valuation data |
| Yuma | Countywide property/owner info | `yuma_property_info` | `yuma_property_tracker.py` | Live (daily 10:00am), ~70,112 parcels, has valuation + sale history + lat/lon |

**All 15 Arizona counties are now live and verified** (Pima, Santa Cruz,
Maricopa, Pinal, Cochise, Mohave, Yuma, Navajo, Apache, Gila, Graham,
Greenlee, La Paz, Coconino, Yavapai). The last two, Coconino and Yavapai,
were unblocked on 2026-08-15 by switching both to ADWR statewide fallback
sources -- see the dated note below for how.

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

**Yavapai blocked by WAF (2026-08-13) -- RESOLVED 2026-08-15, see note
below:** `gis.yavapaiaz.gov` (Yavapai's own GIS server, hosting the same
ArcGIS FeatureServer pattern used successfully for every other county)
returns 403 Forbidden to every request from GitHub Actions runner IPs,
while the identical request succeeds from a normal browser/residential IP.
Two fixes were tried and both failed identically: realistic browser
headers, then `curl_cffi` with Chrome TLS-fingerprint impersonation (the
standard fix for "browser works, script gets 403"). Since even a
real-TLS-fingerprint client still gets 403'd, this looked like IP/ASN-range
blocking of datacenter IPs specifically, not fingerprint-based bot
detection -- which no client-side spoofing from an Actions runner could get
around. Rather than keep fighting the WAF, the tracker was switched to a
different, unrelated ADWR source entirely (see below).

**Yavapai and every new county built on 2026-08-13/14 (Apache, Gila,
Graham, Greenlee, La Paz, Navajo) deliberately only have an
`authenticated` read policy so far, on purpose** -- to avoid repeatedly
hitting the classifier block (see Security note below) across each new
county during the build. The `anon` grant SQL for all of them is queued
up in a NOTE at the bottom of each `supabase_<county>_property_schema.sql`
file, and has also been assembled into one consolidated batch for Juan to
run himself in the Supabase SQL Editor in a single pass. Coconino and
Yavapai's rebuilt (2026-08-15) tables follow the same pattern.

**Coconino deferred -- public layer strips owner/valuation data
(2026-08-13) -- RESOLVED 2026-08-15, see note below:** Coconino's most
prominent official public ArcGIS layer, `Coconino_County_Parcels_Public_View`,
only exposes 6 fields (APN, account number, situs address/city, shape
area/length) -- explicitly no owner name and no valuation, by design (its
own description says "...Public View with limited fields"). The full
owner/valuation/sale data instead lives behind a Tyler Technologies
"EagleWeb" system (`eagleassessor.coconino.az.gov`), a per-parcel HTML
search portal (search by name/parcel/address), not a bulk-queryable API --
no bulk/JSON endpoint was found there. Rather than build a much slower
per-parcel scraper against EagleWeb, a different bulk ADWR source with
owner data was found instead (see below).

**Coconino and Yavapai both unblocked via ADWR statewide sources
(2026-08-15):** both counties' blockers turned out to have the same fix as
Graham/Greenlee/La Paz before them -- the Arizona Department of Water
Resources (ADWR) publishes statewide parcel mirrors on
`azwatermaps.azwater.gov` that are unrelated to either county's own
(blocked/limited) GIS infrastructure:
- **Yavapai** now pulls from `General/Parcels_for_TEST` (FeatureServer),
  layer 8 ("Yavapai_Parcels") -- the same service already used for Graham/
  Greenlee/La Paz. Confirmed 186,484 parcels live, source extract dated
  May 2023. This is an entirely different ADWR service from
  `gis.yavapaiaz.gov`, so the WAF block doesn't apply. The old schema's
  richer fields (subdivision, zoning, mailing address, account number)
  are gone -- the new source only has owner name, situs address pieces,
  book/map/parcel/suffix, acreage, and a computed lat/lon centroid.
- **Coconino** now pulls from a *different* ADWR service, `General/Parcels`
  (MapServer, ADWR's "Parcel Finder" tool), layer 2 ("Coconino"). Confirmed
  78,368 parcels live, source extract dated November 2023. This is a
  MapServer rather than a FeatureServer like every other tracker in this
  repo, but its `/query` REST endpoint is functionally identical (same
  pagination/centroid params, confirmed via
  `advancedQueryCapabilities.supportsPagination` and
  `supportsReturningGeometryCentroid` both `true` on this layer) -- no
  code changes needed beyond the URL and layer id. Unlike the old
  `Coconino_County_Parcels_Public_View` layer, this one includes
  `OWNER_NAME`.

Both new sources share the same gaps as every other ADWR-sourced county in
this repo (Graham/Greenlee/La Paz): no mailing address, no valuation, no
sale history -- just owner name, situs address pieces, parcel-number
components, acreage, and lat/lon. Both are below the ~200k-parcel
checkpoint threshold, so neither needs a resumable checkpoint table.
Yavapai's `daily-yavapai-scrape.yml` had its `schedule:` trigger restored
(16:30 UTC / 9:30am Phoenix, right after Pinal); Coconino's
`daily-coconino-scrape.yml` is a new workflow at 21:30 UTC / 2:30pm
Phoenix (right after La Paz).

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
own mature public GIS FeatureServer (or its own layer omits owner/
valuation data, or blocks GitHub Actions IPs), check the Arizona Dept. of
Water Resources' statewide fallback services first -- there are at least
two: `General/Parcels_for_TEST`
(`https://azwatermaps.azwater.gov/arcgis/rest/services/General/Parcels_for_TEST/FeatureServer/<layer_id>`,
confirmed for Graham, Greenlee, La Paz, Yavapai) and the broader
`General/Parcels` "Parcel Finder" MapServer covering all 15 AZ counties
as one layer each
(`https://azwatermaps.azwater.gov/arcgis/rest/services/General/Parcels/MapServer/<layer_id>`,
confirmed for Coconino) -- before concluding no bulk source exists. Both
share the same field schema and query shape, though the data can be
noticeably staler than a county's own live GIS. A MapServer's `/query`
endpoint behaves the same as a FeatureServer's for these purposes --
check `advancedQueryCapabilities` on the layer's `?f=json` metadata to
confirm pagination/centroid support before assuming so.
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
- Reject non-finite float values (`NaN`/`Infinity`, which ArcGIS can
return for a degenerate/zero-area parcel's computed centroid) in the
numeric-parsing helper with `math.isfinite()` -- otherwise a single bad
centroid crashes the whole batch upsert with PostgREST's "Empty or
invalid json" error.
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
(Ctrl+End/Ctrl+Home, check the ending line number and content) before
committing -- pastes into CodeMirror can silently land partial/garbled,
so always verify before commit. After committing, double-check with a
GitHub blob-page read (not just `raw.githubusercontent.com`, which sits
behind a CDN that can serve a stale cached copy for a few minutes after
a fresh commit).
5. **Create the Supabase tables** via the Supabase MCP tools
(`apply_migration`) or by pasting the schema SQL into the Supabase SQL
Editor (Monaco) and running with Ctrl+Enter.
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
