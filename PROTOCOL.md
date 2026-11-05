# How the Urja Meter Ops portal works

These are notes on what the portal does underneath, worked out by watching its
network traffic and reading its front-end JavaScript bundles. Everything here
was observed on 4 Nov 2026 against `https://urja-ops.flockenergy.tech`. Where
a statement is an inference rather than an observation, it says so.

This document details the investigation into how the Urja Meter Ops
portal works underneath, based on reverse-engineering the network traffic,
SvelteKit frontend bundles, and backend endpoints.

## TL;DR

* It's a **SvelteKit** app, and authentication is done by **better-auth**.
* The UI is a thin shell over a set of **undocumented JSON endpoints under
  `/portal/*`**, so almost no HTML scraping is needed.
* There is a **bulk export** (`/portal/export`) that returns every meter with
  its full hierarchy and coordinates in one response. It requires an
  **HMAC-SHA256 request signature**, and the signing secret is handed to any
  logged-in user by `/portal/keys`.
* Sessions last **exactly one hour from login** and are not extended by use.
* There's a **rate limiter** (HTTP 429, no `Retry-After` header).
* The data is deliberately messy: two different detail-record formats,
  numbers sent as strings, blank hierarchy fields, a renamed transformer,
  duplicate consumption rows, and codes that are reused across parents.

## How I found things

1. I logged in through the browser and listed the network requests made by the
   meters page. That turned up `GET /portal/meters/search?q=&page=1` (JSON) and
   `GET /meters/__data.json` (SvelteKit's data channel).
2. I fetched every JS chunk referenced by the app entry
   (`/_app/immutable/nodes/*.js`) and grepped for `/portal/` and `/api/`. Each
   route's component calls `fetch()` directly, so this produced the complete
   endpoint list, including the export flow and its signing function, without
   clicking anything.
3. I called each endpoint by hand, both with and without the session cookie,
   with odd parameters, and with unknown ids, to see its edge behaviour.
4. I pulled the full export and every meter's consumption series, then
   profiled the data (shapes, blanks, duplicates, consistency between levels).

## Authentication

| | |
|---|---|
| Library | better-auth (endpoints under `/api/auth/*`; sign-out link in the layout bundle is `/api/auth/sign-out`) |
| Login (what the UI does) | SvelteKit form action: `POST /login`, form-encoded `email`, `password`. With `x-sveltekit-action: true`, it answers `{"type":"redirect","status":303,"location":"/meters"}` on success and `{"type":"failure","status":401,...}` on bad credentials. |
| Login (what this service uses) | `POST /api/auth/sign-in/email` with JSON `{"email","password"}`. This is better-auth's native endpoint, and it rejects an empty body with `400 VALIDATION_ERROR`. An `Origin` header is sent because better-auth and SvelteKit both enforce origin checks on POSTs. The form action is kept as a fallback. |
| Session | `HttpOnly` cookie (not readable from JS, so `document.cookie` is empty). `GET /api/auth/get-session` returns `{session:{expiresAt, createdAt, updatedAt, ...}, user:{...}}`, or `null` when logged out. |
| Lifetime | `expiresAt = createdAt + 1h`. After several hundred requests, `updatedAt` was still equal to `createdAt`, so the session is **not sliding**. The client re-logs in 2 minutes before `expiresAt`, and also reactively whenever a request is rejected. |
| Unauthenticated responses | `/portal/*` returns **401** `{"error":"unauthorized","message":"A valid session is required."}`. `/<page>/__data.json` returns **200** `{"type":"redirect","location":"/login"}` (a 200, not a 401). HTML pages redirect to `/login`. |

## Endpoints

All of these are `GET`s and need the session cookie.

| Endpoint | Returns | Notes |
|---|---|---|
| `/portal/meters/search?q=&page=N` | `{data:[{meterId, serialNo, make, phaseType, installStatus, dtCode}], total, page, pageSize}` | Fixed **20 per page**, and `pageSize=` is ignored. `q` is a substring match on **meter id or serial only** (`Genus`, `DT-001` and `faulty` all return 0). `page=0` is treated as 1. A page past the end returns `data: []` with the real `total`. |
| `/meters/{id}/__data.json` | SvelteKit page data (devalue-encoded, see below) | The meter detail page. Carries `detail` (in one of two formats) and `hierarchy` as display labels such as `"Feeder 1 (F-001)"`. |
| `/portal/meters/{id}/geo` | `{data:{latitude:"26.93…", longitude:"75.83…"}}` | Coordinates come as **strings**. |
| `/portal/meters/{id}/energy` | `{data:[{timestamp, kwh, kvah, voltR}]}` | All values are **strings**, see "Consumption data". |
| `/portal/dts?page=N` | `{data:[{code, name, feederCode, capacityKva}], total:40, pageSize:20}` | The transformer register. Two pages. |
| `/portal/keys` | `{data:{signingSecret:"…"}}` | Same value on every call during the session. |
| `/portal/export?page=1` + signature headers | `{data:[…all meters…], total:403}` | **The bulk path.** Ignores `page`. See below. |

Meter ids are **case-sensitive**: `j100000` is a 404. The API upper-cases ids
before calling the portal.

Unknown meter: `/portal/meters/J999999/*` returns **404**
`{"error":"not_found",...}`. But `/meters/J999999/__data.json` returns **200**,
with the page node set to `{"type":"error","status":404,...}`. You have to look
inside the body.

### SvelteKit `__data.json` encoding

The page data uses SvelteKit's `devalue` "flattened" format. The node's `data`
is a flat array, index 0 is the root, and every object or array member is an
**index** into that array:

```json
[{"meterId":1,"detail":2}, "J100000", {"data":3}, ...]
```

Repeated values are stored once and referenced twice. For example, the status
string is shared by `detail` and `hierarchy`. `app/portal/parsing.py::devalue_unflatten`
decodes this format.

### The bulk export and its signature

The Transformers page has an "Export" button. The code behind it, from the
`nodes/4.*.js` bundle and lightly de-minified:

```js
const secret = (await (await fetch('/portal/keys')).json()).data.signingSecret
const ts  = String(Math.floor(Date.now() / 1000))
const msg = ['GET', '/portal/export', 'page=1', ts].join('\n')
const sig = hex(HMAC_SHA256(secret, msg))
fetch('/portal/export?page=1', { headers: { 'x-timestamp': ts, 'x-signature': sig } })
```

Observed behaviour:

* Missing or wrong signature → **401** `{"error":"signature_invalid"}`.
* A correctly signed request whose timestamp is 10 minutes old → **401**. There
  is a freshness window, and the client signs every request fresh.
* The query string is part of the signed message. Signing an empty query and
  calling `/portal/export` with no query also works, and returns the same data.
* `page=2` returns exactly the same 403 rows. The parameter is cosmetic.
* The response holds everything the per-meter endpoints have and more:
  `installType`, `build`, the full structured hierarchy (`{name, code}` per
  level) and numeric `geo`.

Python's `hmac` output was checked against the browser's `crypto.subtle` output
on a known input; see `tests/test_client.py::test_signature_matches_portal_algorithm`.

> Security observation (not exploited, just noted): the signature authenticates
> nothing beyond what the session cookie already does, because any logged-in
> user can read the secret from `/portal/keys`. It acts as a speed bump, not a
> control.

**Why this matters:** one export request replaces about 1,200 requests (403 ×
detail + geo + search pages), which is a big deal under a rate limit. The
whole index rebuild costs **4 portal requests** after login: 2 transformer
pages, keys, and the export.

## Rate limiting

* Crawling consumption with 4 requests in flight hit **429**
  `{"error":"rate_limited","message":"Rate limit exceeded; slow down."}` after
  about 160 requests in well under a minute.
* There is no `Retry-After` header. Requests succeeded again within about 2 seconds.
* Short bursts of 30–40 parallel requests, or about 2 requests per second
  sustained, never triggered it. My inference is a per-session or per-IP
  window of roughly 150 requests per minute. I didn't probe further, so as not
  to hammer the portal.
* The client throttles itself to 2 req/s and retries 429, 5xx and network
  errors with exponential backoff and jitter (1s, 2s, 4s, 8s).

## Data model

* **403 meters**, `J100000`–`J100402`. Statuses: 238 Installed, 90 Faulty, 75
  Decommissioned. Makes: HPL 90, Genus 88, Secure 85, Allied 76, L&T 64. 277
  single-phase, 126 three-phase. 205 Whole Current, 198 CT Operated.
* **40 distribution transformers** (`DT-001`–`DT-040`), with capacities of 63–400 kVA.
* **Hierarchy:** Zone (3) › Circle (6) › Division (10) › Subdivision (14) ›
  Substation (18) › Feeder (28) › DT (40) › Meter.
* Each meter has a `build` of `legacy` (238) or `v2` (165), which decides the
  shape of its detail record.

### Two detail-record formats

`legacy` meters store a list of name/value pairs:

```json
"detail": {"data": [{"parameterName": "Serial No", "parameterValue": "SE33962"}, ...]}
```

`v2` meters store a **JSON string inside JSON**, with PascalCase keys:

```json
"detail": {"classData": "{\"installed_meter\":{\"SerialNo\":\"SE65293\",\"InstallationStatus\":\"Faulty\",...}}"}
```

The portal's own UI branches on `"classData" in detail`, and so does
`parsing.normalise_meter_detail`.

### Hierarchy quirks

Every meter carries its own copy of its full path, and that's where the mess is:

1. **Blank fields.** 22 meters have one level with an empty `code` (14) or an
   empty `name` (8), affecting the circle, substation or feeder level. For
   example, `J100044` has `feeder: {"name":"Feeder 5","code":""}`.
2. **A renamed transformer.** The register calls `DT-007` "Sanganer DT 7".
   Meters `J100400`–`J100402` (the last three ids, and the only `GN…`
   serials, so probably a later batch) call it **"Old Malviya Nagar Xfmr"**.
   That's why the total is 403 rather than a round 400.
3. **Codes are reused across parents.** `SD-01` appears under divisions
   `D-01`, `D-05` and `D-09`. `F-001` appears under `SS-01` and `SS-11`.
   `DT-029` is fed by `F-001`, not `F-029`. The levels look like independently
   numbered sequences, not a tree with globally unique codes. Grouping by bare
   code would silently merge unrelated branches.
4. **Apart from those blanks, every meter on a given DT reports the same
   path,** and the feeder agrees with the register's `feederCode` for every DT.

How the API deals with this (`app/hierarchy.py`):

* **The DT is the anchor**, because every meter has a `dtCode` and it always
  matches the register. For each DT and each level, the API takes the most
  common non-blank code among its meters. The register's `feederCode` wins if
  it disagrees (it never did).
* Blank codes and names are filled from sibling meters on the same DT. The
  register's DT name wins, and other names are kept as `aliases`.
* Tree nodes are keyed by **full path** (`Z-01/C-01/D-07`) and keep the raw code
  as a field.
* Every repair is recorded. It shows up as `quality_flags` on the meter, and in
  `/hierarchy/issues`. The unrepaired path is still available as
  `portal_hierarchy`.

### Consumption data

* Each reading is a **cumulative register value** (kWh, kVAh), plus an R-phase
  voltage. Consumption is the difference between consecutive readings. The
  registers never go backwards in this dataset, but the parser flags it if they do.
* There are **two resolutions**. `J100000`–`J100039` (40 meters) have 337
  half-hourly readings from 23/06/2026 23:30 to 30/06/2026 23:30. The other 363
  meters have 8 daily readings at 00:00, from 23/06 to 30/06.
* Timestamps are `DD/MM/YYYY HH:MM` **with no timezone**. I assume IST (the
  utility is in Jaipur), and the API emits `+05:30` ISO-8601.
* **Duplicate trailing rows:** 5 meters (`J100089`, `J100134`, `J100195`,
  `J100211`, `J100330`) repeat their final timestamp. The second copy has blank
  `kwh` and `voltR`. The API keeps the more complete row and reports the duplicate.
* Voltage is always 220–240 V. There were no gaps beyond the native resolution
  and no non-numeric junk.
* **Decommissioned meters still have consumption readings.** That's either a
  data-quality problem or a status that hasn't been updated. The API flags it
  rather than guessing which.
* The data is a fixed historical window (late June 2026) and doesn't change
  between calls. Per-meter consumption has no bulk path: one request per meter.

### Smaller observations

* Serial-number prefixes don't correspond to the make. `SE…` is used for HPL,
  Genus and others, and some serials start with `L&`. Don't infer make from serial.
* `geo` is a string in `/geo` and a number in the export.
* The search endpoint doesn't return `installType`, `build`, hierarchy or geo,
  so it can't replace the export. It's used only as a degraded fallback.
