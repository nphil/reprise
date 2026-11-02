# Reprise playback integration

Create a named client in Reprise's owner interface. Copy its secret once; only its hash is retained. Send it in `Authorization: Bearer <secret>`, never in a URL. The default scopes are `results:read` and `jobs:read`; `analysis:request` is optional. These credentials cannot review, configure, publish, or stream media. Rotate or revoke a lost secret from the owner interface. Responses are `Cache-Control: no-store`.
Rotation revokes the old client and returns a **new client ID** with the replacement secret. Use the returned ID for subsequent rotation or revocation; the old ID is not an alias for the replacement.

The machine-readable contract is [reprise-openapi.yaml](reprise-openapi.yaml). The generic probe `tools/reprise_probe.py` sends a single saved-result lookup, does not enqueue analysis and does not print credentials. Set `REPRISE_TOKEN` in the environment and pass the Reprise base URL and all eight playback identity fields (see `--help`). Only use a trusted HTTPS connection, or a trusted local network.

## Playback safety

Supply the exact Plex machine identifier, rating key, media ID, part ID, token-free part key, part size in bytes, original-file duration in milliseconds and selected audio stream ID. Missing or mismatched identity is unresolved; do not guess a version. Lookups use saved results only and do not decode or schedule work. Only act on `eligible_skip_ranges` when resolution is `resolved`, status is `complete`, and freshness is `current`. Other states, including unapproved analysis, have no eligible skips. Ranges are half-open `[start_ms, end_ms)` in original-file presentation milliseconds, not transcoded timestamps. The `terminal` boolean is explicit; `false` is the default. Never infer it from proximity to the end.

**Marquee handoff:** Marquee's existing final-marker heuristic treats a marker near `duration - 2000ms` as final. Do not apply that heuristic to Reprise ranges: a brief scene or gag may follow a skip. Respect each exact end and explicit `terminal` value. Integrating and deploying Marquee changes is a separate task; no Marquee code is changed here.

Owner routes under `/api/reprise` use the existing owner authentication and CSRF protection. Review only saves decisions; publication requires separate prepare and apply operations and is disabled by default. A publication undo can refuse if another writer changed the data. Errors do not include private paths or secrets.

Owner `POST /api/reprise/refresh` accepts `{"server_id":"…","library_id":"…"}` and returns HTTP 202 with a job ID and `operation: "refresh"`. Poll `GET /api/reprise/jobs/<id>`; use `POST /api/reprise/jobs/<id>/pause`, `/resume`, or `/cancel` to control it. Completion includes `refresh` counts for registered, skipped, and removed entries. A cancelled, failed, or incomplete scan never removes entries it did not see; skipped files make the refresh incomplete. A refresh only indexes the library and does not run analysis.

All Reprise job responses identify `operation` as `analysis`, `preview`, or `refresh`. The boolean `pause_requested` reports the job pause request, not proof that running work has already stopped. Terminal states are `completed`, `failed`, and `cancelled`. Only owner credentials can request library refreshes.

## Reanalysis and job controls

Recurrence references must belong to another Plex episode. Alternate files or audio bindings sharing the target's machine identifier and rating key are not references.

When evidence changes, reanalysis replaces untouched detector proposals, including provisional keeps. Manual ranges, review overrides and locks survive on the same media generation; retained skip overrides lose approval until reviewed again. An unchanged rescan retains its revision and approvals. This lets a newly available reference produce reviewable skips without obsolete detector keeps blocking them.

Cancellation and result finalization share one job-state decision. If cancellation wins, the active analysis becomes cancelled and its old skip approvals are cleared. If the last result commits first, the job completes before a late cancel can change its state. Resume starts work only when an existing pause is successfully removed; completed, failed and cancelled jobs remain terminal. To run them again, explicitly request a new analysis.
