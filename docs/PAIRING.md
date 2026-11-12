# App connections: QR or paste

Deployed November 11, 2026: **0.4.10 / contract 1.6**. The signed Mac console,
Mac receiving apps, and six receiving apps on the owner's iPhone and iPad are
installed. Existing credentials remain valid. See rollout, inventory and rollback.
Physical scan/permission acceptance remains separate from installation.

## User flow

1. In Home Services Status on the Mac, open **App Connections**. Its existing
   SSH connection administers the Pi; no additional admin token is required.
2. Choose the receiving app/service, name the device, select permissions, and
   choose **Generate QR Code**.
3. In that app, open **Pi Connection** and choose **Scan QR Code** or paste the
   copied invitation. Review the server and permissions, then **Connect**.
4. The app saves its device-specific credential in Keychain. Its settings show
   permission toggles and, when supported, upload-folder and camera pickers.
   The app's existing provider adapter uses the new credential and preferences.
5. The Mac lists connected devices, last use and access. Permission changes
   apply on the next authenticated request. Disconnect revokes the token.
   Already-issued media sessions retain their existing bounded expiry.

Both devices still need LAN/tailnet connectivity. Pairing configures a
connection; it does not install Tailscale, expose the Pi publicly, authorize
an operating-system camera prompt, or make an offline server reachable.
The system camera app does not route to a particular app automatically:
scan **inside the receiving app**. Paste works on Mac as well as phones.

## Access and destination choices

Permissions use the existing server scopes. An invitation can approve a subset
of a registered preset; an app can subsequently toggle only within that
original approval. Disabling all feature scopes still permits authentication
and connection settings so they can be enabled again. Use Disconnect to revoke
the credential completely. Each device gets a separate credential; apps do not
share a universal token.

`upload_folder` and `camera_id` are **client preferences, not resource access
restrictions**. The server validates that the folder exists on the guarded Files
volume and that the camera is configured. It creates neither a folder nor a
camera. Applications must apply these defaults in their own adapters. An app
with file-read access still has access to the managed Files root; a camera
default does not hide other authorized cameras. Folder-scoped authorization,
per-app storage quotas and arbitrary filesystem destinations are not implemented.

## Protocol 1

The QR code and clipboard contain identical `homeservices://pair#<base64url-JSON>`
bytes. The JSON contains `protocol: 1`, `provider: "home-services"`, `base_url`,
`server_id`, `invitation_id`, `secret`, `preset`, and `expires_at`. The secret is
a 256-bit, five-minute invitation, **never a durable API token**. Only its hash
is stored. Treat an unused invitation as a temporary credential. A copied
invitation is removed from the Mac clipboard at expiry if it is still current.
Do not put real invitations or tokens in tickets, screenshots, Git or logs.

| Endpoint | Authorization | Behavior |
| --- | --- | --- |
| `POST /api/v1/pairing/preview` | `{invitation_id, secret}` in JSON | Server identity, name, preset, approved scopes and expiry; no token |
| `POST /api/v1/pairing/exchange` | Same proof plus `client_secret`, `device_name` in JSON | Issues one app token and connection receipt |
| `GET /api/v1/connection` | App's bearer token | Own connection, original approval, enabled scopes and preferences |
| `PATCH /api/v1/connection` | App's bearer token | Replace enabled `scopes` and/or `preferences`, bounded by original approval |
| `DELETE /api/v1/connection` | App's bearer token | Revoke own credential |

The client generates 32 cryptographically random bytes, base64url without
padding, as `client_secret`. Save that secret and invitation privately **before**
exchange. The resulting token has the existing format `hst_<token-id>.<client-secret>`.
Only hashes persist on the server. Until invitation expiry, the identical
invitation/client secret/device name can replay a dropped response. A different
claim returns `409 pairing_used`; revocation never resurrects a token. SQLite's
immediate transaction serializes competing claims, including separate gateway
processes. Clients must not generate a new secret for each retry.

Both POSTs are authenticated by the high-entropy invitation; no short numeric
code endpoint exists. Invitation proof never appears in an HTTP URL. The server
returns `Cache-Control: no-store` on pairing and connection responses, and the
access log records only method/path/status. The Apple client requires HTTPS,
refuses redirects, verifies server ID/address/preset, uses ephemeral networking
and stores both pending and completed credentials in per-app Keychain records.
Expired/cancelled invitations return `401 pairing_expired`; ordinary tokens
and all earlier endpoints remain compatible. Unmanaged tokens get 404 from
`/connection` until adopted, while their existing API calls continue working.

## Operator CLI

Run on the service host using its actual configuration. `invite` prints an
ephemeral secret for local QR rendering; other commands do not disclose secrets.

```sh
home-services --config /etc/home-services/config.toml connections catalog
home-services --config /etc/home-services/config.toml connections invite \
  --preset camera-monitor --name 'Camera Monitor · iPhone' \
  --base-url 'https://<actual-host>.<tailnet>.ts.net:8443'
home-services --config /etc/home-services/config.toml connections list
home-services --config /etc/home-services/config.toml connections cancel PAIR_ID
home-services --config /etc/home-services/config.toml connections adopt TOKEN_ID --preset camera-monitor
home-services --config /etc/home-services/config.toml connections update CONN_ID --scope cameras:read
home-services --config /etc/home-services/config.toml connections update CONN_ID --disable-all
home-services --config /etc/home-services/config.toml connections revoke CONN_ID
```

`update --scope` replaces the entire enabled set. Omit it to leave scopes alone.
`--preferences-json '{"camera_id":"actual-camera-id"}'` replaces preferences.
An existing token can be adopted only if active and every current scope fits
the selected preset. Adoption retains its secret, expiry and scopes. Its maximum
approval is its current scope set, not the preset's larger set. Do not adopt
admin credentials into an app. The Mac management UI uses the same commands
with shell-quoted arguments over existing SSH host trust.

## Adopt in existing apps

The standalone Swift package at [`clients/HomeServicesConnection`](../clients/HomeServicesConnection)
has no sibling-project or external-package dependency. See its README for
embedding, credential observation, camera permissions, and tests. Add it to
each app's own repository/release. Copy or vendor a versioned package; a relative
path into this checkout must not become a required sibling dependency.

| App / backend | Preset | Installed integration |
| --- | --- | --- |
| Home Page → Home Services | `home-page` | Mac Settings → Raspberry Pi; adds a separate readiness card |
| Camera Monitor → Home Services | `camera-monitor` | Mac/iPhone/iPad Connections → Pi Connection; adds its own provider profile and applies default camera |
| Files & Backups → Home Services | `files-backups` | Mac/iPhone/iPad Settings → Pi Connection; real file/backup adapter, default browse/upload folder and share-extension default |
| Content Manager phone → article relay | `content-manager` | iPhone/iPad Article synchronization → Pi Connection; existing relay adapter, separate paired credential and preserved manual setup/draft caches |
| Chew Sense Collect → encrypted mailbox | `chewsense-phone` | iPhone/iPad Mac Connection → Home server mailbox → Pi Connection; preserves the Mac-supplied encryption key |
| Opportunity Review → direct Pi backend | `opportunity-review`, provider `opportunity-review` | iPhone/iPad Settings → Pi Connection; separate scoped credentials on 8444, real `MacClient`/widget adapter |
| Casino Exit → Pi runner | `casino-exit`, provider `casino-exit-runner` | iPhone/iPad Settings → Auto Apply runner → Pi Connection; separate scoped credentials on 8768, real runner adapter |
| Mac reporting/article/Chew Sense companions and Home Platform | Existing presets | Ten existing Home Services credentials adopted for management without rotation. Their service setup/secret files remain intact. |
| Older Opportunity Home Services relay | `opportunity-review` / `opportunity-companion`, provider `home-services` | Existing credentials preserved and managed; this does not replace the direct backend. |

Choose the matching **Backend** in the Mac console before generating an invitation.
The app chooses its provider in code, then checks the invitation's provider, preset,
HTTPS origin and server identity. Credentials never move between these backends.
All three backends expose the same five pairing/connection endpoints above.
Independent backends reuse the stdlib engine and retain their own databases,
permissions and endpoint checks. Opportunity Review grants `review:read`,
`review:write`, `outbox:schedule`, `discovery:refresh`; Casino Exit grants
`runner:read`, `runner:control`, `runner:submit`. The existing exact approval,
recipient verification and submission checks still run after scope checks.

The shared client retains saved state during outages, refuses redirects and
reports expired/used invitations, revoked credentials and Keychain failures.
Its host callback hands credentials to the actual app adapter and reports a
retryable error if that step fails. A generation check prevents late network
responses from restoring a forgotten connection. The host cancels a previous
asynchronous handoff when the saved credential changes.

Manual providers remain available. New Camera/Files profiles and the Home Page
card do not overwrite old profiles. Content Manager, Opportunity Review and Casino
Exit store paired credentials separately from their prior manual credentials;
disconnecting does not silently resume an older credential. To deliberately
restore manual setup, use its existing setup flow (Content Manager also offers
**Restore Saved Manual Connection**). Article drafts/outbox stay in their original
server/workspace caches. Chew Sense must first learn the recording encryption key
from its paired Mac for the same Pi: an invitation supplies HTTP access only.
The original Mac offer remains intact, and a later offer does not undo an explicit
paired-mailbox disconnect. Existing companion credentials are managed on the Mac;
new headless-companion setup still uses each service's own setup command.

## Future services and release

For a new Home Services-backed app, register its least-privilege scopes in
`auth.SCOPES` and preset in `auth.PRESETS`, enforce those scopes at every endpoint,
then regenerate the contract. Both the CLI and Mac catalog discover presets
from that registry. New data shapes/storage behavior remain the owning service's
responsibility; QR pairing alone is not a generic data store or backup engine.

For independent Pi backends, retain their own ownership and credentials. Reuse
the versioned invitation-envelope idea and UX with a distinct provider adapter.
The Apple client binds a provider constant supplied by the host app; it never
selects a provider from untrusted invitation contents. See the standalone package
README and `clients/PiConnectionBackend` for the adapter contract.

Rollout used the exact installed gateway source plus the reviewed pairing files,
and exact installed independent-backend source plus their pairing adapters. Core
workers, storage, backup schedules and camera producers were not restarted.
Existing recovery support was already deployed and was preserved. Additive tables
live on system storage; older releases ignore them. Rollback changes code only and
retains current live state. See exact release/rollback evidence.

## Validation

```sh
.venv/bin/python -m pytest tests/test_connections.py tests/test_connected_apps.py \
  tests/test_config_network_auth.py tests/test_api_contract.py -q
.venv/bin/home-services contract --out contracts/v1
.venv/bin/python tests/check_swift_pairing.py
swift test --package-path clients/HomeServicesConnection --scratch-path build/ConnectionPackage
xcodegen generate --spec mac-status/project.yml
xcodebuild -quiet -project mac-status/HomeServicesStatus.xcodeproj \
  -scheme HomeServicesStatus -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath build/DerivedData-mac CODE_SIGNING_ALLOWED=NO build
```

Fixture checks cover expiry/cancellation, no secret disclosure, scope isolation,
adoption, revocation, lost-response replay, competing claims and guarded defaults.
Physical scan accuracy, camera permission denial/recovery, Keychain on signed
devices, background/relaunch behavior and real tailnet reconnects require device
acceptance. Temporary live acceptance credentials were revoked; production credential secrets and scopes were preserved.
