# Native Silo plugin

This is a native Go port of the existing bridge, packaged with Silo's public
plugin SDK v0.32.1. One self-contained Linux amd64 binary supports both Silo's
plugin protocol and standalone HTTP serving. It does not need Python or the
existing bridge container.

## Deployment status

On 2026-11-04 the binary was installed into the live Silo server as
`community.tracearr-bridge`, version `0.2.0`, installation **9**. Configuration
was saved through Silo's settings API. Its own listener started on port **8098**,
and the plugin verified authenticated `/Sessions` requests over loopback.

On 2026-11-05 the host's LAN listener on port 8098 was externally reachable.
Liveness, readiness, Jellyfin admin verification, session listing, and discovery
of all three configured libraries returned HTTP 200. The plugin reported the
same Silo server ID as the Python bridge. No sessions were active at that check.
The remaining deployment step is updating the existing Tracearr server URL and
verifying playback through it. The Python bridge on 8097 remains available
until migration is confirmed.

## Build

From the repository root, with Docker on Linux amd64:

```sh
docker build --platform linux/amd64 --target artifact \
  --output type=local,dest=plugin/dist plugin
```

Outputs:

- `plugin/dist/tracearr-bridge-linux-amd64` — upload this binary to Silo.
- `plugin/dist/manifest.json` — self-describing manifest with binary checksum.
- `plugin/dist/SHA256SUMS` — SHA-256 checksum.

With Go 1.26 installed, `cd plugin`, `go test -race ./...`, then:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w' \
  -o dist/tracearr-bridge-linux-amd64 .
```

The manifest currently declares Linux amd64 only. Do not use this binary on an
ARM server; that needs its own build and matching platform declaration.

## Install and configure

1. In Silo's plugin administration, upload the binary. It identifies itself as
   **Tracearr Bridge**. Uploading another binary with the same plugin ID replaces
   the installed version.
2. Save its **Bridge settings**:
   - Native API URL: normally `http://127.0.0.1:8080` **inside the Silo container**.
   - Silo API key: an administrator-owned key. Scoped keys need
     `admin:monitor:read`, `libraries:read`, and `admin:sessions:control`.
   - Bridge API key: the separate key Tracearr uses. Reuse the existing bridge
     key when migrating.
   - Listen address: `0.0.0.0:8098`.
   - Server name: the display name you want.
3. Add `"8098:8098"` to the **Silo service's** existing Compose `ports:` list.
   Recreate that service to apply the mapping. No extra bridge container is
   needed for this listener.
4. Start/check it through the admin status route:
   `/api/v2/plugin-content/plugins/<installation-id>/status`.
   This route requires a Silo admin session or unscoped admin key; it does not
   use the bridge key. It starts the listener if needed and checks both liveness
   and authenticated session access without terminating any playback.
5. From a machine on Tracearr's network, request
   `http://<Silo-host-LAN-IP>:8098/health/live`. It should return JSON with
   `"status":"ok"` and `"version":"0.2.0"`.
6. Change the existing Silo entry's URL in Tracearr to
   `http://<Silo-host-LAN-IP>:8098`, keeping the same bridge API key. Server,
   account, item, and media-file IDs are intentionally preserved from the Python
   implementation. Verify playback, artwork, library sync, and termination
   before stopping the old bridge container.

If Tracearr and Silo share a Docker network, `http://<silo-service>:8098` can be
used instead of a host-published address. A public HTTPS reverse proxy may also
target this listener, but it must preserve the authentication header. Changing
the public Silo hostname's port alone does not create a Docker mapping.

### Lifecycle limitations

Silo treats ordinary plugins as lazy-started processes. Only network-access
providers currently have dedicated resident-process supervision; this plugin
does not pretend to be a network provider.

- A `scheduled_task.v1` capability starts/checks the listener. Silo's default
  plugin task trigger is **startup**, so a server restart should launch it.
  Keep that task enabled.
- The admin `/status` route starts it on demand after installation or a settings
  change. Saving settings stops the old plugin process, so invoke status again.
- A plugin process crash closes the listener. A status request or task run can
  relaunch it. For automatic periodic recovery, configure this plugin's task on
  an interval in Silo's task settings. Task-binding changes may require a Silo
  restart; follow the server's response/UI. This is not a resident supervisor.
- Settings' **Check Connection** runs a temporary plugin process. It checks the
  native API only and never binds the listener, so it can coexist with the real
  process. A successful check alone does not launch the real listener.
- The default listener port must be unique per Silo deployment. Multiple API
  processes sharing one network namespace require a deployment-specific design.

When testing cold-start behavior, check port 8098 **before** invoking `/status`;
otherwise that request could hide a startup-task problem. External reachability
is verified; a controlled Silo cold-start test remains to be performed.

## Administrator helper

The root `tools/plugin_admin.py` uses only Silo's supported admin APIs. It reads
credentials from environment variables or, with `--from-compose`, the existing
bridge's Compose environment; it does not print secrets.

```sh
python3 tools/plugin_admin.py --from-compose install plugin/dist/tracearr-bridge-linux-amd64
python3 tools/plugin_admin.py --from-compose configure 9
python3 tools/plugin_admin.py --from-compose status 9
```

Replace `9` with the ID returned by install on another server. `configure`
defaults to localhost:8080 and port 8098; override with `--native-url` and
`--listen`. Installation refuses to overwrite an existing plugin unless
`--replace` is supplied. The tool does not change networking or Tracearr.

## Verification and compatibility

```sh
# Existing HTTP integration contracts against the Go listener:
python3 tools/verify_go_bridge.py
# Real Tracearr parsers against responses fetched through that listener:
python3 tools/verify_tracearr.py /path/to/tracearr --go
```

Go tests exercise an actual go-plugin subprocess: handshake, manifest
validation, empty/new-install configuration, settings checks, startup-task
launch, authenticated listener access, concurrent temporary check, process
shutdown, and relaunch on the same port. HTTP tests cover playback observations,
users, libraries, catalog queries, artwork, termination, auth, and failure paths.

The existing functionality is ported: monitoring, movie/TV catalog, account-level
identity, playback posters, and termination. It has the same functional limits
as the Python bridge: no SSE, historical import, general player controls, or
full-library artwork. The Go port retains at most 16 playback poster entries
(8 MiB per fetched image) in memory. Source paths and other unavailable metadata
are not fabricated.

## Standalone mode

The same binary runs outside Silo:

```sh
export SILO_URL=http://silo-host:8090
export SILO_API_KEY=your-silo-key
export BRIDGE_API_KEY=your-separate-bridge-key
export BRIDGE_LISTEN=0.0.0.0:8098
./plugin/dist/tracearr-bridge-linux-amd64 serve
```

Without `serve`, it speaks the SDK's plugin protocol. `manifest` prints metadata
and exits without starting a listener or reading credentials.
