# Reflection

This document covers the core findings, engineering decisions, and practical
trade-offs encountered during the investigation and implementation of the API.

## What assumptions did you make?

The ones that affect results are listed in the README's "Assumptions"
section. The important ones:

* Timestamps are IST. The portal gives no timezone, and the utility is in Jaipur.
* kWh and kVAh are cumulative registers. Every series rises steadily, and
  reading them as interval values would give absurd numbers (48,000 kWh per
  half hour), so consumption is the difference between consecutive readings.
* The transformer register is the source of truth for DT names and feeders. The
  per-meter copies of the hierarchy are what drift, and they always agree with
  the register wherever they aren't blank.
* When two rows share a timestamp, the complete one is the real reading. The
  duplicate always has blank kWh and voltage and an identical kVAh.
* "Decommissioned but still reporting" is a data problem to surface, not
  something to correct, because there's no way to know which side is wrong.

## Which part was the most difficult, and how did you get unstuck?

Interpreting the hierarchy. The first consistency check said almost every
level was broken: `SD-01` sat under three different divisions, `F-001` under
two substations, and so on. That looked like a very dirty dataset, and the
obvious move was to "repair" it by majority vote per code. That would have
quietly rewritten correct data.

What got me unstuck was printing the first 45 meters one per line with their
full path. The pattern was obvious once laid out: each level counts through its
own range (zones 1–3, circles 1–6, divisions 1–10, and so on) independently of
its parent. So the paths are consistent, and the codes just aren't globally
unique. The real dirt was much smaller: 22 blank fields and one renamed
transformer. That led to anchoring on the DT, keying nodes by full path, and
repairing only the blanks.

A practical challenge: exploring the legacy portal involved inspecting the
frontend bundles and network transactions within the browser session. Because
the target portal is rate-limited and proprietary, I captured sample responses
to construct a high-fidelity fake portal for offline unit and integration testing.
That allowed deterministic testing of edge cases (rate limits, backoff, expired sessions,
malformed payloads). `scripts/smoke_test.py` was then built for end-to-end verification
against the live portal.

## If you had another day, what would you improve?

The fleet-wide consumption job and a time-series table. The interesting
operational questions are which transformers are near capacity, which meters
are using much more than their neighbours, and which "decommissioned" meters
are clearly live. They all need every meter's readings in one place. That
means a background job that pulls 403 series within the rate limit (about
3.5 minutes at 2 req/s) and stores them properly. Per-transformer load
compared with `capacity_kva` would be the first thing to build on that.

After that, I'd add scheduled contract tests that compare the fake portal with
the real one, so format drift (a `v3` record format, say) gets caught by CI
instead of by a user.

## What mistake did you make while solving this?

While profiling consumption data, the first crawl ran 4 requests in parallel
with a naive "retry 3 times, then move on" loop. It hit the rate limiter, and
the loop **silently skipped 65 meters**. The first data profile was computed on
338 meters and looked complete. It was caught only because the total was
compared with 403 before any conclusions were drawn. The rerun was slow and
sequential.

Two lessons went straight into the code: the client throttles itself and backs
off rather than racing, and a failed request is never silently dropped. It
either raises an error or serves a cache marked `stale`.

(A smaller one: the consumption endpoint first read the meter's status from
the index without making sure the index was loaded, so `install_status` was
missing on a cold start. A test caught it.)

## If you were reviewing your own submission, what would you criticise?

* **The fake portal was written by the same author, from the same
  understanding, as the client.** If something about the portal was
  misunderstood, the tests will happily agree with the mistake. The live smoke
  test is the only guard, and it isn't in CI.
* **The JSON login endpoint was inferred**, not observed directly in the network log. The UI uses the
  SvelteKit form action, and `/api/auth/sign-in/email` was chosen based on better-auth's standard API conventions.
  A fallback to the form action is implemented in the client, but live verification requires credentials via `smoke_test.py`.
* **Some of the rate-limit numbers are inferences.** "About 150 per minute"
  comes from one initial burst test rather than exhaustive boundary profiling.
* `main.py` is getting long enough that consumption mathematics and aggregation routines should eventually be extracted into a dedicated service module.
* The dashboard needs internet access for its external CDNs and OpenStreetMap tiles.

## My takeaways (Aakarshan)

1. **Hierarchy resolution requires deep data empathy**: Initial consistency checks indicated widespread data corruption across divisions and substations. It was easy to jump to conclusions and "correct" data by majority vote. Instead, stepping back to inspect raw hierarchical patterns revealed that codes were simply parent-scoped rather than globally unique. Anchoring on the DT and tracing full parent paths solved the problem without falsifying valid data.
2. **Defensive API integration in hostile environments**: When wrapping legacy systems with no public documentation, rate limiting (HTTP 429 without `Retry-After`), short-lived sessions (fixed 1-hour expiration), and silent pagination failures require rigorous client design. Adding client-side rate throttling, jittered exponential backoff, proactive re-authentication, and stale cache serving (`X-Data-Stale`) creates a resilient system that protects both the backend and downstream consumers.
3. **Preserve data provenance instead of silent mutation**: When encountering anomalous readings (such as decommissioned meters continuing to report consumption or missing fields), surfacing these anomalies via structured flags (`quality_flags`, `issues`) is vastly superior to silently dropping or overwriting them. It gives upstream operators actionable data quality insights while keeping downstream contracts reliable.
