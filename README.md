# Silo–Tracearr bridge

A standalone service that presents Silo's native monitoring API as
the Jellyfin API consumed by Tracearr. Written in Python 3.12+, with no runtime
dependencies. This is an independent integration, not an official Silo plugin.

**Native plugin prototype:** the [Go plugin implementation](plugin/README.md)
can now run inside Silo and has been installed and checked on the live server.
Its separate listener uses port 8098 and is verified reachable on the Silo
host's LAN address. Updating the existing Tracearr server URL and verifying
playback are the remaining migration steps; the Python service documented
below remains available on port 8097.

## What works

- Add the bridge to Tracearr as a **Jellyfin** server using an API key.
- Poll **all native Silo playback sessions**, including native apps and
  Jellyfin-compatible clients, through Silo's administrative monitoring API.
- Track account, device identity (when reported), IP address, media title,
  position, pause state, source format, and direct-play/remux/transcode decisions.
- Synchronize login accounts and movie/TV libraries, movies, series, episodes,
  external IDs, and available file-version metadata.
- Keep stable server, account, media, and file IDs across bridge restarts.
- Show currently playing posters through an authenticated Jellyfin image route,
  using the signed artwork URL supplied by Silo.
- Terminate streams from Tracearr by revoking the native Silo session's playback
  authority, with the termination reason delivered when the player supports it.

Tracearr records history from the sessions it observes. It controls the polling
interval; the bridge reads Silo on each `/Sessions` request. Silo's built-in
Jellyfin listener is not required.

## Quick start (Docker)

1. Create an **administrator-owned Silo API key** under Silo's admin settings.
   If your version offers scope selection, select `admin:monitor:read` and
   `libraries:read`. If it offers no scope selection, use the regular admin key:
   Silo treats unscoped keys as having their owning account's full access, and
   the bridge accepts them too.
   For **Terminate Stream**, scoped keys also need `admin:sessions:control`.
   Unscoped admin keys already have this access.
2. Copy the example settings:

   ```sh
   cp .env.example .env
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```

3. Edit `.env`: set `SILO_URL`, `SILO_API_KEY`, and use the generated value for
   `BRIDGE_API_KEY`. `SILO_URL` must point to the **native API**, not the Jellyfin
   port. Silo's current example Compose file publishes native API port **8090**
   on the host (container port **8080**).

   Use an address reachable **from the bridge container**, such as
   `http://192.168.1.10:8090`. `http://silo:8080` works only when both services
   share a Docker network. `localhost` inside the container is the bridge itself.

4. Build and check the connection:

   ```sh
   docker compose build
   docker compose run --rm silo-tracearr-bridge python -m silo_tracearr --check
   docker compose up -d
   ```

5. In **Tracearr → Settings → Servers**, add a **Jellyfin** server:
   - **URL:** `http://<bridge-host>:8097`
   - **API key:** the value of `BRIDGE_API_KEY`
   - **Name:** your preferred Silo server name

   Tracearr must be able to reach that address from its own container. Leave
   the optional public address unset; this bridge does not serve a Jellyfin UI.

Start a movie in Silo and wait for Tracearr's next poll. Check logs with
`docker compose logs -f`.

## Run directly

From this directory, export the same environment variables and run:

```sh
python3 -m silo_tracearr --check
python3 -m silo_tracearr
```

The Python command does not read `.env` itself; Compose loads that file. You can
also install with `python3 -m pip install .` and use `silo-tracearr-bridge`.

| Variable | Meaning | Default |
| --- | --- | --- |
| `SILO_URL` | Native Silo API base URL, optionally with reverse-proxy base path | required |
| `SILO_API_KEY` | Admin-owned Silo key; unscoped or with the monitoring scopes above, plus `admin:sessions:control` for termination | required |
| `BRIDGE_API_KEY` | Separate token used only by Tracearr | required |
| `BRIDGE_HOST` | Listen address (keep `0.0.0.0` in Docker) | `0.0.0.0` |
| `BRIDGE_PORT` | Listen/published port | `8097` |
| `BRIDGE_NAME` | Display name in server information | `Silo (Tracearr bridge)` |

## Current limits

This is a first implementation, tested with simulated Silo HTTP responses,
Tracearr's actual response parsers, and live Silo playback/poster requests.
Initial server connection and playback tracking have also been reported working
in Tracearr; other features still need broader live validation.
It targets Silo's current v2 monitoring endpoints, which older releases may lack.

- **Account-level tracking:** household profiles are grouped under their login
  account. A device's ID is omitted when Silo does not report one, rather than
  inventing a new device for every session.
- **Polling only:** no Tracearr SSE endpoint, CPU/RAM charts, or library push
  events. Tracearr's optional SSE probe receives 404 and can use polling.
- **Termination only:** no pause/resume, general-purpose messaging, account
  changes, media playback, or member sign-in. Control support is advertised only
  when Silo's command capabilities include authority-revoking termination.
- **Playback artwork only:** posters are discovered from active sessions. Only
  same-origin native Silo artwork URLs are supported; external artwork/CDN URLs
  are omitted. The last 16 fetched images are cached in memory (up to 8 MiB each),
  with up to 512 URL registrations. After a bridge restart, active sessions can
  restore artwork; older history relies on Tracearr's own image cache. No general
  library artwork sync or avatars yet. Image resizing parameters are accepted
  but the Silo-provided image is served at its existing size.
- **No historical import:** no Playback Reporting plugin emulation, activity-log
  translation, or pre-existing watch history.
- **Movie/TV catalog only:** no standalone season rows, music, ebooks, or
  audiobooks in library sync. Live observations are translated using the media
  type Silo reports; Tracearr may not recognize other media types.
- Silo's monitoring catalog does not provide every Jellyfin field: source paths,
  genres, catalog runtimes, and exact dimensions may be absent. Unknown data is
  omitted. Live sessions include runtime when Silo provides it.
- The bridge version is reported honestly (`0.1.0`), so Jellyfin version/update
  notices in Tracearr do not describe the installed Silo version. Jellyfin media
  deep links generated by Tracearr do not open Silo's native UI.
- Catalog snapshots are cached for 60 seconds (users for 30, libraries for 60).
  A full catalog refresh is bounded to 25 seconds and 200,000 rows; live session
  pagination is bounded to 8 seconds. Very large/slow catalogs may need a future
  persistent background index. Failed upstream reads return 502, never a
  successful empty collection that could imply deletions or stopped playback.

## Health and troubleshooting

- `GET /health/live`: public process liveness; used by Docker's health check.
- `GET /health/ready`: authenticated upstream identity, scope, and capability
  checks. `--check` also exercises live-session mapping and user/library reads.
- **Silo HTTP 401/403:** verify the upstream key is administrator-owned. If it
  has explicit scopes, it needs both scopes above; an unscoped admin key also
  works. Tracearr needs the *bridge* key, not the upstream key.
- **Silo HTTP 404:** check the native API port/base URL and whether the installed
  Silo version exposes `/api/v2/admin/sessions/capabilities` and
  `/api/v2/admin/catalog/capabilities`.
- **No session location:** verify Silo itself reports the real `client_ip`;
  configuring trusted reverse proxies belongs on Silo.
- **Wrong city/location:** Tracearr geolocates Silo's `client_ip`, which the bridge
  passes through unchanged. A proxy/VPN address therefore shows that server's
  location. For reverse proxies, forward the actual client address in
  `X-Forwarded-For` and configure Silo's `clientip.trusted_proxies` setting (or
  `SILO_TRUSTED_PROXIES` environment variable) to trust the actual proxy hop.
  Silo defaults to trusting private networks and loopback, not public proxy IPs.
  Preserve the existing trusted hops when adding a proxy, and start a new
  playback session afterward to verify the newly recorded address.

## Terminate Stream

Tracearr checks the Jellyfin session's control flags and then sends
`POST /Sessions/{id}/Playing/Stop`. The bridge translates this to
`POST /api/v2/admin/sessions/{id}/terminate`. Success requires Silo to confirm
`authority_revoked: true` and `durable_state: stopped` for that session.

This works even without a player's realtime control connection: Silo rejects
further media-token use and progress writes. It attempts to notify the player,
but already-buffered media may play briefly afterward. A missing notification
is not a termination failure when Silo confirms revocation.

Tracearr sends its optional termination reason in a preceding Jellyfin `Message`
request. The bridge acknowledges it as queued (202), retains it for up to 60
seconds, and includes it in the termination request. It does not display that
message separately. At most 256 pending reasons are held in memory.

The server capability probe cannot establish whether a scoped credential has
write permission. If the button fails with 403, add `admin:sessions:control` to
the upstream key or use an unscoped administrator key. Upstream failures and
unconfirmed revocations never return successful termination responses.

The bridge accepts Jellyfin `Authorization: MediaBrowser ... Token="..."`,
legacy `X-Emby-Authorization`/`X-Emby-Token`, and API-key query parameters.
It never forwards those credentials to Silo, logs query strings, or follows
upstream redirects. Its only upstream credential is `SILO_API_KEY`.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
# Optional: requires a Tracearr checkout, Node/npm, and network for pinned esbuild.
python3 tools/verify_tracearr.py /path/to/tracearr
```

The HTTP tests use a local native-Silo test server to exercise authentication,
multi-page users/sessions/catalog, playback transitions, consistent IDs,
upstream failures, and invalid pagination. The optional contract test bundles
the real Tracearr parsers and checks mapped playback/catalog data through them;
it does not modify the checkout or install Tracearr's workspace dependencies.

Source contracts inspected on 2026-11-04:

- [Silo `e3af98b`](https://github.com/Silo-Server/silo-server/tree/e3af98b06af2ee0ced92da518bf372f4b9fd5df6):
  `internal/apiv2/admin_sessions.go`, `admin_users.go`, `admin_catalog_items.go`,
  `user_libraries.go`, `system_identity.go`, and API-key scope middleware.
- [Tracearr `25cb413`](https://github.com/connorgallopo/tracearr/tree/25cb41383452392bfab62dba3fec29e2526da6c4):
  Jellyfin client, shared media-server client, and Jellyfin/Emby parsers.

See [the native plugin documentation](plugin/README.md) for its build,
configuration, startup behavior, and verification results.
