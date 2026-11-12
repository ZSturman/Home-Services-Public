# Home Services API, contract v1

This is the boundary Camera Monitor and Files & Backups implement against.
Clients own their models and adapters; nothing here requires this source
checkout. The machine-readable sources are committed with the server:

- [`contracts/v1/openapi.json`](../contracts/v1/openapi.json): the OpenAPI document generated from the running app.
- [`contracts/v1/examples/`](../contracts/v1/examples/): 107 recorded request/response pairs from
  the real gateway against isolated fixtures. These include error cases, volume
  loss, idempotent replays, and media-session expiry.

Each example file has `name`, optional `note`, `request` (`method`, `path`,
`headers`, `query`, `body` or `body_bytes`), and `response` (`status`,
selected `headers`, `body`, `body_kind`). The fixtures use deterministic IDs
(`job_0000000000000001`), a fixed clock (2026-10-20T08:00:00Z), the
placeholder host `https://host.example.net:8443`, and fixture credentials
(`hst_tok_….fixture-secret-0001`). No real hostnames or tokens appear in them.
Restic snapshot IDs, capacity bytes, and file times are real values from the
fixture run, so they change between regenerations.

`tests/test_api_contract.py` replays the scenario on every test run. It
fails if the OpenAPI document changes or if any example's status, error code,
or JSON shape stops matching live behavior. In that check a `null` field
matches any shape.

## Connection and versioning

- Base URL: the HTTPS URL the owner configures, for example
  `https://<host>.<tailnet>.ts.net:8443`. There is no default host.
- Auth: `Authorization: Bearer hst_<id>.<secret>`. Tokens are app-specific and
  scoped; store them in the client's Keychain. A missing, malformed,
  expired, or revoked token returns `401 unauthenticated`. A valid token
  without the needed scope returns `403 forbidden` with
  `details.required_scope`.
- Send `Home-Services-Contract: 1`. Every response carries
  `Home-Services-Contract: 1.6` (1.1 added move, single-entry lookup,
  protection, the camera recorder routes, and the Home Platform backup
  routes; 1.2 added external backup sources and camera snapshots; 1.3 added the private article review relay; 1.4 added the opportunity review relay; 1.5 added the Chew Sense mailbox; 1.6 adds QR/paste pairing and app connection settings; nothing
  earlier changed). Minor versions only add; use
  `bootstrap.contract.version` to decide whether to call a 1.1 route. Any other major version returns
  `400 contract_version_unsupported` with `details.supported_majors`.
- Errors always look like this:
  `{"error": {"code": "...", "message": "...", "details": {...}}}`.
  Branch on `code`; `message` is for people and may change.
- Unsupported features are never an empty success. They return
  `409 capability_unavailable` with a `reason`, and
  `GET /api/v1/capabilities` lists each one as `"state": "unavailable"`.

| Scope preset (`home-services tokens create --preset`) | Scopes |
| --- | --- |
| `files-backups` | storage:read, storage:write, transfers:write, jobs:read, jobs:write, backups:read, backups:restore |
| `files-backups-companion` | backups:report, backups:read, jobs:read |
| `camera-monitor` | cameras:read, cameras:control, media:read, jobs:read |
| `home-platform-legacy` | legacy:storage-contract |

## Bootstrap

| Request | Purpose | Example |
| --- | --- | --- |
| `GET /healthz` (no auth) | `{"status":"ok"}` only, with no detail | 01 |
| `GET /api/v1/bootstrap` | stable `server_id`, version, contract, roots, cameras, token scopes | 02 |
| `GET /api/v1/capabilities` | what this host can do now, with reasons for anything unavailable | 03 |
| `GET /api/v1/readiness` | per-module state (volumes, roots, backups, cameras, notifications, state store) | 04 |

Cache `server_id` to recognize the same server across URL changes.

## Pairing and app connections (1.6)

Owner-generated five-minute invitations exchange for the existing scoped tokens.
The QR/paste payload includes the server origin and a temporary invitation
secret; it never contains the final credential. Pairing POSTs use that secret
in JSON instead of bearer authentication. `/api/v1/connection` exposes only the
authenticated app's settings. Examples 98–107 cover preview, exchange/replay,
competing claims, permission bounds, revocation and expiry. Full request shapes,
CLI administration and client requirements: [PAIRING.md](PAIRING.md).

## Files (`files` root only)

```
GET  /api/v1/storage/volumes                      volume state: verified | missing | wrong_volume | wrong_fstype
GET  /api/v1/storage/capacity                     free bytes minus the 10 GB reserve and outstanding uploads
GET  /api/v1/storage/files/entries?path=&limit=&cursor=
GET  /api/v1/storage/files/entry?path=            one entry {name,path,kind,size,modified_at} or 404 (1.1)
GET  /api/v1/storage/files/content?path=          download; supports Range (206)
POST /api/v1/storage/files/directories   {"path": "Photos"}
POST /api/v1/storage/files/move          {"from": "a.txt", "to": "Docs/b.txt", "conflict": "fail"|"keep_both"}   (1.1)
POST /api/v1/storage/files/trash         {"path": "Documents/a.txt"}      -> managed Trash (7 days)
GET  /api/v1/storage/files/trash
POST /api/v1/storage/files/trash/{id}/restore     restores with keep-both naming
```

Paths are relative to the root. Absolute paths, `..`, NUL, and symlink
escapes return `403 path_not_allowed`. Symlinks and special files are never
listed. A missing or wrong disk returns `503 volume_unavailable` with
`details.state` set to `missing`, `wrong_volume`, `wrong_fstype`, or
`root_missing` (the `error_volume_*` examples). Move is a same-volume
rename: the destination's parent must exist (`404 parent_missing`), a
directory cannot move inside itself (`403`), an existing destination is
`409 destination_exists` unless `keep_both`, and case-only renames work on
case-insensitive volumes such as exFAT. Show this as "storage unavailable," not as an
empty folder. Permanent delete is not offered.

## Resumable uploads

```
POST   /api/v1/transfers/uploads            Idempotency-Key: <client transfer id> (optional, recommended)
       {"root":"files","path":"Documents/x.txt","size":63,"sha256":"<hex>","conflict":"keep_both"|"fail"}
PUT    /api/v1/transfers/uploads/{id}/content   Upload-Offset: <n>   body: application/octet-stream (≤ 16 MiB)
GET    /api/v1/transfers/uploads/{id}           current state and acknowledged offset
POST   /api/v1/transfers/uploads/{id}/complete  {"create_parents": false}
DELETE /api/v1/transfers/uploads/{id}           cancel; the partial file is removed
```

1. Create the session. It reserves space, and `201` returns `offset: 0`. Replaying the
   same `Idempotency-Key` with the same request returns the original session
   with `200` (example 13); a different request returns `409 idempotency_mismatch`.
2. Send chunks at the server's offset. The response returns the new
   `Upload-Offset`. If the offset is wrong, you get `409 offset_mismatch` with
   `details.expected_offset` (example 15). After a disconnect, call `GET` and
   resume from `offset`.
3. Complete. The server re-hashes the staged file and checks the size and
   SHA-256 if you supplied one. It then renames the file into place on the
   same volume. `verification.sha256` is `matched` (you sent a hash) or
   `computed_by_server`. On a collision, `keep_both` publishes `name (1).ext`
   (example 18); `fail` returns `409 destination_exists` and the session stays
   open.
4. Errors: `409 checksum_mismatch` (nothing published), `413 size_exceeded` or
   `chunk_too_large`, `507 insufficient_space`, `507 staging_cap_exceeded`
   (20 GB aggregate cap), `503 volume_unavailable` (the disk was lost; the
   acknowledged offset is kept). Sessions idle for 24 hours become
   `abandoned` and their partial files are deleted.

## Jobs

```
GET  /api/v1/jobs?state=&kind=&limit=
GET  /api/v1/jobs/{id}
POST /api/v1/jobs   Idempotency-Key required   {"kind":"storage.cleanup","params":{}}
POST /api/v1/jobs/{id}/cancel
```

Job states are `waiting`, `running`, `blocked` (for example, the volume is
missing; it retries later), `succeeded`, `failed`, `cancelled`, and
`interrupted` (the worker's lease lapsed or it stopped). `heartbeat` (the
worker is alive) and `progress` (the work advanced) are separate objects,
each with `at` and `age_seconds`. Treat a stale progress age as "no recent
progress," not as a failure. Jobs run in separately supervised workers, not
in the gateway.

## Backups and recovery

Since server 0.4.7, an explicitly configured `companion_recovery` root and private
`backup.companion_password_file` also expose current Mac companion snapshots through
the existing routes below. Snapshot IDs select the correct recovery repository;
ambiguous prefixes return `409 ambiguous_snapshot_id`. With this reader enabled,
snapshot records include an additive `repository` label (`primary` or `companion`),
and repository status includes `recovery_repositories`. Protection groups include
actual companion snapshots. Primary writer receipts/retention remain isolated from
the recovery reader, and external run reports retain their reported evidence.
See mobile recovery for setup and credential boundaries.

```
GET  /api/v1/backups/repository                   present | missing | unavailable; writer lease; integrity/retention are "not_owned"
GET  /api/v1/backups/snapshots?source_id=
GET  /api/v1/backups/snapshots/{id}/entries?path=/abs/dir
GET  /api/v1/backups/snapshots/{id}/content?path=/abs/file     streamed archived-file download
POST /api/v1/backups/restores   Idempotency-Key required
     {"snapshot_id":"<hex>","include":["/abs/path/in/snapshot"]}  -> job (kind backup.restore)
```

`GET /api/v1/backups/protection` (1.1) lists every snapshot series in the
repository (host, paths, tags) with its newest snapshot, `state`
(`current` | `overdue`), `age_seconds`, `contents` (`files`, `bytes` from the
snapshot's own summary), and `empty` (true when the newest snapshot contains
no files: the backup "succeeds" but protects nothing). Snapshots include the
same `contents` field. `retired` (additive, server 0.4.2) is `null`, or
`{"reason": ...}` for a series the operator listed in
`[[backup.retired_series]]`: nothing writes it any more, its snapshots stay
browsable and restorable, `state` still reports its real age, and it raises
no overdue/empty alerts or legacy-status warnings.

A restore always writes into a fresh `files/Restores/<job id>/` directory
and never overwrites anything. restic keeps the snapshot's absolute paths
under that directory (example 35, `result.destination`). Home Services never
initializes, forgets, or prunes the repository.

### Files & Backups Mac companion (outbound only)

```
POST /api/v1/backups/sources            {"host","path","tag","scheduler":"files-backups-companion","repository":"primary"|"external:<label>"}
GET  /api/v1/backups/sources[/{id}]     protection: current | overdue (> 26 h) | none | unknown (repository unreadable)
POST /api/v1/backups/sources/{id}/runs  Idempotency-Key required  {"scheduler":"files-backups-companion"}
POST /api/v1/backups/runs/{run}/heartbeat   {"progress": {...}}   renew at least every lease_seconds (900)
POST /api/v1/backups/runs/{run}/finish      {"state":"succeeded","snapshot_id":"<64 hex>","summary":{...}}
GET  /api/v1/backups/runs/{run}
```

- Each source has one scheduler. Registering with another scheduler returns
  `409 source_owned_by_other_scheduler`; the operator transfers ownership
  with `home-services backups set-scheduler`.
- Starting a run takes the single repository writer lease. A second writer
  gets `409 repository_busy`. The response includes
  `repository.locator` (a non-secret restic locator for the companion's own
  restic process) and `lease_seconds`.
- `finish` with `succeeded` is accepted only for a snapshot the repository
  contains with the source's tag and path (`409 snapshot_not_found`,
  `409 snapshot_mismatch`, or `503 snapshot_unverifiable`; the run stays
  live so the companion can retry or report the real outcome). The stored
  `snapshot_id` is the repository's full ID. Protection is computed from the
  repository's snapshots, so it is `current` even for series that were never
  reported, and a reported success cannot make a source look protected.
- `repository: "external:<label>"` (1.2) registers a status-only source for a
  repository Home Services cannot read (for example the companion's own
  repository, whose key stays on the Mac). Runs use the same protocol with
  their own writer lease; `succeeded` records the reported `snapshot_id`
  without verification, `summary.total_files`/`total_bytes` become
  `protection.contents`, and protection reports
  `evidence: "companion_report", verified: false`. These sources raise
  overdue/failed alerts but never appear in `/backups/protection`.
- A run whose lease lapses becomes `interrupted`. Any later report for it
  returns `409 run_not_active` (example 42) and can never replace a newer
  success. Restic's own locks still apply.

## Cameras and media

With `cameras.engine = "none"` every camera capability is
`unavailable / engine_not_configured`. With the native `hls-recorder`
engine (deployed on the Pi) capabilities reflect what is true now.

```
GET  /api/v1/cameras, /api/v1/cameras/{id}
PUT  /api/v1/cameras/{id}/armed {"armed": true}                    arming = motion notifications; recording runs either way
GET  /api/v1/cameras/{id}/snapshot?width=64..1280                  -> image/jpeg, newest live frame (1.2)
POST /api/v1/cameras/{id}/live-sessions                            -> 201 {url: ".../index.m3u8", protocol: "hls", expires_at, expiry: "sliding"}
GET  /api/v1/cameras/{id}/recordings?limit=&cursor=&kind=motion|continuous
GET  /api/v1/cameras/{id}/recordings/{rid}
POST /api/v1/cameras/{id}/recordings/{rid}/playback-sessions       -> 201 {url: ".../index.m3u8", protocol: "hls", expires_at}
POST /api/v1/cameras/{id}/recordings/{rid}/exports                 -> 201 {download_url, url, filename, content_type: "video/mp4", size_bytes, expires_at}
PUT  /api/v1/cameras/{id}/recordings/{rid}/protected {"protected": true}   motion recordings only
DELETE /api/v1/cameras/{id}/recordings/{rid}                       -> 204 | 409 recording_active | 409 recording_protected
GET  /api/v1/media/legacy/events                                   predecessor events, read_only: true, ISO-8601 times
POST /api/v1/media/sessions {"kind":"legacy_event_media","event_id":"...","name":"recording.mp4"}
GET  {session url}[/{name}]                                        no Authorization header
```

A camera: `id`, `name`, `kind`, `engine`, `armed`, `status`
(`state`: `recording` | `stale` | `offline` | `recorder_unavailable` |
`storage_unavailable` | `not_managed`, plus `reason`, `observed_at`,
`last_video_at`, `recording`, `armed`), and `capabilities.<name>` =
`{state: "available"|"unavailable", reason?, message?}` for `live_view`
(`protocol: "hls"`), `snapshot` (`format: "jpeg"`, `max_width`), `recording`, `motion_events`, `history`, `playback`
(`protocol`), `export` (`format: "mp4"`), `deletion`
(`granularity: "recording"`), `notifications` (`transport: "ntfy"`), and
`arming` (`effect`). A healthy-but-not-recording state, if ever added, will
be `idle`.

Recordings are listed newest first: `motion` (`evt_…`, one per motion event,
padded by 5 s) and `continuous` (`hr_YYYYMMDDHH`, one per UTC hour), each
with `started_at`, `ended_at` (null while `active`), `size_bytes`,
`protected`, `playable`. Playback is an HLS VOD playlist (EVENT while
active) of the stored MPEG-TS chunks. Export remuxes the window to MP4
without re-encoding (seconds per event). Deleting removes the recording's
stored chunks except any that overlap a protected or active recording.
Retention: minutes without motion 24 h, motion and protected recordings
30 days, all under `limits.camera_quota_bytes`. Legacy predecessor media is
never modified.

Session URLs are opaque, grant exactly one resource, and need no token (for
AVPlayer); never put the API token in a URL. Live sessions slide: they stay
valid while the player keeps fetching (2 min idle, 12 h cap). Playback
sessions last 4 h, export downloads 1 h, legacy media 15 min. An expired
session returns `401 media_session_expired`. `legacy_sync_claim` repeats
the predecessor's `synced` flag; it does not verify that a copy exists.

## Notifications (admin)

`GET /api/v1/notifications/outbox` and `POST /api/v1/notifications/test`
(Idempotency-Key required). Deliveries retry with exponential backoff
(15 s doubling to an hourly cap, 20 attempts, about 13 hours) and show
`last_error`; the ntfy URL is never echoed or logged.

The notifier's monitor announces state changes once per episode, after a
grace period, and announces recovery: storage disk unavailable/available,
backup series overdue or empty, repository unreadable, job not responding,
camera not recording, restore finished or failed, companion backup run
failed, and motion (only while the camera is armed, at most one per
cooldown).

## Legacy Home Platform route

`GET /api/storage/contract` returns the predecessor's nine fields.
`GET /api/backup/destination` returns the predecessor's disk view (state,
device, uuid, mount, filesystem, repository, usage). `GET /api/backup/status`
returns `state` (`completed` | `running` | `failed` | `unknown`), `warning`
(overdue or empty series), and `error`, derived from repository snapshots,
which are the fields Home Platform reads. All three accept
`X-Workspace-Token: <token>` or a bearer token with the
`legacy:storage-contract` scope, and were compared field by field with the
predecessor on the Pi. `mode` is `production` or
`pending`, never `test`, and `syncthing_workspace_folder` is `unknown`
(see `src/home_services/legacy.py`).

## Regenerating

```sh
.venv/bin/home-services contract --out contracts/v1   # needs restic on PATH for the backup examples
.venv/bin/python -m pytest tests/test_api_contract.py
```

## Article review sync (1.3)

`POST /api/v1/articles/review` provides scoped cached article reads and a durable, idempotent, revision-bound review queue. The Mac remains the canonical writer; no publisher/generation runs on the gateway. See ARTICLE_SYNC.md for action schemas, token presets, quotas, identity/replay semantics and recovery. Generated examples 78–87 exercise this boundary.

## Opportunity review relay (1.4)

`POST /api/v1/opportunities/relay` holds read-only copies the Opportunity Discovery Mac companion publishes and a durable, idempotent queue of revision-bound iPhone requests, which may be chained with `after`. The Mac applies them through its own routes and returns receipts. Sending, dispatch and test-receipt routes cannot be queued. See OPPORTUNITY_RELAY.md. Generated examples 88–97 exercise this boundary.
