# Recent Crossmatch API

A read-only HTTP endpoint that returns the catalog crossmatches for objects that
had an alert in a recent time window, grouped by object, at a caller-selectable
level of detail. It exists to feed scientist-facing demos and notebooks (for
example, "show me the crossmatches for everything with an alert in the last
night of observing") without a database login.

## Endpoint

```
GET https://crossmatch.scimma.org/api/recent-crossmatches
```

This endpoint is **public and unauthenticated** — no credentials are needed. A
per-page object cap and a maximum window span bound the work any one request can
trigger, and a per-source-IP rate limit at the edge bounds request rate: a client
that exceeds it is rejected with `429` and should back off and retry. Retry with
exponential backoff and jitter, honoring `Retry-After` if the response carries
it, and give up after a small fixed number of attempts rather than retrying
indefinitely.

**Authentication and authorization will be required in a future release.**
No date or mechanism is fixed yet. Write clients so that credentials can be added
later without restructuring the request path.

Results are **keyset (cursor) paged**: each response returns one operator-bounded
page plus an opaque `next_cursor`; following the cursor to exhaustion returns the
window's entire distinct set of matched objects. There is no total-object cap and
no "jump to page N" — see [Paging](#paging).

This endpoint shares the service's response contract **additively**: every
field it returned before keeps its name, type, and meaning, and pages now also
carry a `provenance` block and an `as_of` pin (see [Response](#response)), and
errors carry structured keys beside the original `error` string (see
[Errors](#errors)). Write clients to ignore response keys they do not know.

### Related endpoints and machine-readable docs

This endpoint answers "what matched recently". To ask about specific objects,
use the other read queries, which also report objects with no coincident source
and objects the service has never seen:

- `GET /api/objects/<diaObjectId>` and `POST /api/lookup` — look up objects by
  `diaObjectId`, sky position, or TNS name, singly or in a batch, with catalog
  filters and `response=count`.
- `GET /api/cone` — every Rubin object the service has seen within a radius of
  a position, paged.
- `GET /api/tns/<name>` — the Rubin objects around a TNS object.
- `GET /api/describe` and `GET /api/status` — catalogs, filterable fields,
  per-request limits, and service availability.

Every operation, parameter, response field, status value, and error code is
described in the OpenAPI 3.1 document at [`/openapi.json`](https://crossmatch.scimma.org/openapi.json).
AI agents should start at [`/llms.txt`](https://crossmatch.scimma.org/llms.txt);
the whole reference is also served as Markdown at
[`/api-docs.md`](https://crossmatch.scimma.org/api-docs.md).

## Query parameters

All parameters are optional.

| Param | Type | Default | Meaning |
|---|---|---|---|
| `start` | ISO-8601 timestamp (UTC) | `end` minus 12 hours | Window start (inclusive). A value with no timezone offset is interpreted as UTC. |
| `end` | ISO-8601 timestamp (UTC) | now | Window end (exclusive). |
| `time_field` | `ingest_time` \| `event_time` | `ingest_time` | Which alert timestamp the window filters on. `ingest_time` is when the alert arrived at the service; `event_time` is the observation/candidate time. |
| `detail` | `ids` \| `position` \| `matches` \| `full` | `matches` | How much per-object and per-match data to return (cumulative; see below). |
| `page_size` | positive integer | 1000 | Maximum number of objects in the page. A value above the operator maximum is **clamped down**, not rejected. |
| `cursor` | opaque string | none | A `next_cursor` from a prior page. Resumes the next page. It **pins** `start`/`end`/`time_field`/`detail`; see [Paging](#paging). |

### Bounds

- **Default page size:** 1000 (`RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE`), used when
  `page_size` is omitted.
- **Maximum page size:** 10000 (`RECENT_CROSSMATCH_MAX_PAGE_SIZE`). A `page_size`
  larger than this is clamped, not rejected.
- **No total-object cap:** there is no limit on how many objects a window can be
  paged through in total; only per-page size is bounded.
- **Maximum window span:** 168 hours (7 days; `RECENT_CROSSMATCH_MAX_WINDOW_HOURS`).
  A `start`/`end` span longer than this returns `400`.
- **Request budget:** 5 seconds of wall-clock time per request
  (`API_REQUEST_BUDGET_SECONDS`), shared with the other read queries. A request
  that would run longer is stopped and answered `400 query_too_expensive`. The
  page-size defaults are unchanged, so a large page at `detail=full` (up to
  10000 objects, each with its full published payloads) can exceed the budget
  on a busy database; if it does, retrying the same request will not help —
  request a smaller `page_size` and follow `next_cursor`.
- **No filters or counts:** this endpoint does not filter or count. A
  filter-named parameter (for example `gaia_dr3.parallax_over_error_min`,
  `catalog`, `separation_arcsec_max`, `reliability_min`, or any name containing
  a dot or ending in `_min`/`_max`) or `response` returns
  `400 unsupported_parameter`, so unfiltered results are never mistaken for
  filtered ones. Use `POST /api/lookup`, `GET /api/cone`, or `GET /api/tns/<name>`
  to filter or count. Any other unknown parameter is ignored.

### Errors

Every error the application returns is a JSON object with a fixed `code` to
branch on, a human-readable `message`, the offending `param` (or `params`, when
several) where one applies, and `retryable`. The original `error` string is
still present with the same text as before (it equals `message`), so clients
that read `error` keep working:

```json
{
  "error": "detail must be one of ('ids', 'position', 'matches', 'full'), got 'everything'",
  "code": "invalid_parameter",
  "message": "detail must be one of ('ids', 'position', 'matches', 'full'), got 'everything'",
  "param": "detail",
  "retryable": false
}
```

| Status | `code` | When | `retryable` |
|---|---|---|---|
| `400` | `invalid_parameter` | An unknown `detail` or `time_field`, an unparseable `start`/`end`, a non-positive or non-integer `page_size`, a malformed `cursor`, a `cursor` presented with a conflicting `start`/`end`/`time_field`/`detail`, an `end` earlier than `start`, or a window span beyond the maximum. | `false` |
| `400` | `unsupported_parameter` | A filter-named parameter or `response` (see [Bounds](#bounds)); `param`/`params` names them. | `false` |
| `400` | `query_too_expensive` | The request exceeded the request budget. The same request would overrun again; ask for less. | `false` |
| `405` | `method_not_allowed` | Any method other than `GET`. | `false` |
| `503` | `service_unavailable` | The service database is temporarily unreachable. The response carries `Retry-After`; retry after that many seconds. | `true` |

A request that exceeds the rate limit returns `429`. This response is produced
by the edge proxy rather than the application, so — unlike the errors above — it
does **not** carry the JSON error body. Detect it by status code alone; do not
parse the body.

## Response

Always a JSON object with the service-level provenance, the resolved query
metadata, paging fields, and an `objects` list. The result is **matches-only**:
an object whose alert is in the window but that has no catalog match is not
included.

```json
{
  "provenance": {
    "service_version": "0.15.0",
    "contract_version": "1.0.0",
    "crossmatch_radius_arcsec": 1.0,
    "catalogs": [{"name": "gaia_dr3", "release": "Gaia DR3"}, "..."],
    "reliability_cuts": [
      {"broker": "pittgoogle", "min_reliability": 0.6, "enforced_by": "service",
       "status": "service_setting", "as_of": null},
      "..."
    ]
  },
  "window": {"start": "2026-07-13T00:00:00+00:00", "end": "2026-07-13T12:00:00+00:00"},
  "time_field": "ingest_time",
  "detail": "matches",
  "page_size": 1000,
  "count": 2,
  "next_cursor": "eyJ0IjoiMjAy...",
  "objects": [ ... ],
  "as_of": "2026-07-13T12:00:03.123456+00:00"
}
```

- `provenance` describes the service answering the request: its version, the
  response-contract version, the configured crossmatch radius, the catalogs in
  service with their releases, and the minimum LSST reliability per broker and
  where it is enforced. It is the same block every other read query returns,
  and it reports the **current** configuration, not the configuration in effect
  when a listed match was made; per-object crossmatch provenance is available
  from `GET /api/objects/<diaObjectId>` and `POST /api/lookup`.
- `page_size` is the effective page size used (after clamping/defaulting).
- `count` is the number of objects **on this page**, not a whole-set total (no
  cheap total exists under keyset paging).
- `next_cursor` is the opaque token to fetch the next page, or `null` when the
  window is exhausted.
- `as_of` pins the walk: only alerts ingested at or before this time are listed,
  on every page of one walk (see [Paging](#paging)).
- `diaObjectId` is a 64-bit integer. JavaScript and other parsers that read
  JSON numbers as doubles lose precision above 2^53; parse it as a 64-bit
  integer.

Each entry in `objects` grows with the `detail` level. The levels are cumulative:
each includes everything the previous level does.

### `detail=ids`

Object identifiers only:

```json
{"diaObjectId": 9000000123, "diaObjectId_str": "9000000123"}
```

### `detail=position`

Adds the alert **object** position (`ra`/`dec` in degrees):

```json
{"diaObjectId": 9000000123, "diaObjectId_str": "9000000123", "ra": 180.0, "dec": -30.0}
```

### `detail=matches` (default)

Adds a `matches` list. Each match names the catalog, the source id in that
catalog, and the angular separation in arcseconds between the alert object and
the catalog source:

```json
{
  "diaObjectId": 9000000123,
  "diaObjectId_str": "9000000123",
  "ra": 180.0,
  "dec": -30.0,
  "matches": [
    {"catalog_name": "gaia_dr3", "catalog_source_id": "42", "separation_arcsec": 0.5}
  ]
}
```

### `detail=full`

Each match is built from the same payload builder the Hopskotch publish path
uses, so it carries the same fields — including the nested `catalog_payload` of
catalog-specific columns. Note that `ra`/`dec` inside a match are the matched
**catalog source** coordinates (`source_ra_deg`/`source_dec_deg`), which differ
from the object's `ra`/`dec` at the object level:

```json
{
  "diaObjectId": 9000000123,
  "diaObjectId_str": "9000000123",
  "ra": 180.0,
  "dec": -30.0,
  "matches": [
    {
      "diaObjectId": 9000000123,
      "ra": 180.0011,
      "dec": -29.9998,
      "catalog_name": "gaia_dr3",
      "catalog_source_id": "42",
      "separation_arcsec": 0.5,
      "catalog_payload": {"phot_g_mean_mag": 18.3, "ruwe": 1.02, "...": "..."},
      "catalogs_skipped": [],
      "partial": false
    }
  ]
}
```

Only the current match version per `(object, catalog, source)` is returned, so a
re-matched object surfaces each match once, not once per version.

**Batch-coverage fields differ from the live stream.** The `catalogs_skipped` /
`partial` batch-coverage fields (see the design doc's published-payload section)
are always `[]` / `false` here. This endpoint serves a *stored* match and has no
record of which catalogs were skipped in the batch that originally produced it,
so it cannot reconstruct that per-batch value — only the live Hopskotch stream
carries the real coverage. A consumer that needs to know whether a given match's
batch was partial must capture it from the stream at publish time.

## Paging

Results are ordered **newest-first** by the selected `time_field`, with
`diaObjectId` as a unique tiebreaker. Each page returns a `next_cursor`; pass it
back as `cursor` to fetch the next page, and stop when `next_cursor` is `null`.
The union of all pages is the window's entire distinct set of matched objects,
each `diaObjectId` appearing exactly once.

- **The cursor is opaque.** Treat it as a token; do not parse or construct it. It
  is `base64url`-encoded and unsigned (it carries only public query parameters and
  a public sort position).
- **The cursor pins the query.** It records the `start`, `end`, `time_field`, and
  `detail` it was issued for. You may omit those params on the follow-up request
  (they are derived from the cursor) or repeat the same values, but presenting a
  **different** value for any of them returns `400` — this prevents a paged walk
  from silently drifting to a different query. `page_size` is **not** pinned and
  may change from page to page.
- **The cursor pins the object set (`as_of`).** The first page sets `as_of`
  to the time of that request, and its cursors carry it, so every page of one
  walk lists only alerts ingested at or before `as_of`, whichever `time_field`
  you walk by. The pin is a bound on ingest time, not a snapshot: ingest time is
  set before an alert's ingest commits, so an alert whose ingest commits while
  you page can still appear on a later page if its ingest time is at or before
  `as_of`. A cursor
  issued before `as_of` existed still works: it resumes from its position, and
  the rest of that walk is pinned from the request that presents it.
- **Statuses can advance between pages.** The pin fixes which alerts are in the
  set, not their crossmatch state. Crossmatching runs continuously, so an alert
  already in the pinned set that gains its first match while you page can
  appear on a later page if its position is still ahead of the cursor. A walk
  is always duplicate-free: an object already returned is never returned again.
  For an exact "pull the whole set", page a closed window whose alerts have
  finished crossmatching (for example, last night once observing has ended and
  the last batch has run).

## Examples

Last 12 hours (defaults), grouped by object with catalog/source/separation:

```
GET /api/recent-crossmatches
```

An explicit window on observation time, ids only:

```
GET /api/recent-crossmatches?start=2026-07-12T00:00:00Z&end=2026-07-13T00:00:00Z&time_field=event_time&detail=ids
```

Full published payload, 200 objects per page:

```
GET /api/recent-crossmatches?detail=full&page_size=200
```

Walk an entire window (follow `next_cursor` until it is `null`):

```
GET /api/recent-crossmatches?time_field=event_time&detail=ids&page_size=1000
  -> {"...": "...", "next_cursor": "eyJ0Ijoi...", "objects": [ ... ]}
GET /api/recent-crossmatches?cursor=eyJ0Ijoi...
  -> {"...": "...", "next_cursor": "eyJ0Ijoi...", "objects": [ ... ]}
...
GET /api/recent-crossmatches?cursor=<last>
  -> {"...": "...", "next_cursor": null, "objects": [ ... ]}   # done
```

A runnable end-to-end example lives in
[`notebooks/recent_crossmatch_demo.ipynb`](../../notebooks/recent_crossmatch_demo.ipynb).
