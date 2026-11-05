# Releases and the standalone plugin repository

Add this repository URL in **Silo → Administration → Plugins → Repositories**:

```text
https://github.com/neilsb/silo-tracearr-bridge/releases/latest/download/repository.json
```

This is an independently maintained repository, not a listing in Silo's trusted
community catalog. The same plugin ID, `community.tracearr-bridge`, is retained
for future distribution changes. Current builds support Linux amd64 only.

## Install or update

Refresh Silo's plugin catalog, choose **Tracearr Bridge**, and install the desired
version. Existing manual installations use the same plugin ID; installing from
the repository should update that installation rather than create a second
bridge. Keep the current configuration and Tracearr URL/key. Open the plugin's
**Tracearr Bridge** diagnostics page to start/check the listener after updating.

For first-time setup, publish port 8098 and follow [plugin setup](plugin/README.md).
Enable the listener task at startup and every 60 seconds. Recovery scheduling is
a host setting; installing a plugin does not automatically change task triggers.

## Publish a version

1. Update the version in `plugin/main.go` and `plugin/manifest.json` together.
2. Update `CHANGELOG.md`, run tests, and commit.
3. Push `main`, create a matching `vMAJOR.MINOR.PATCH` tag, and push that tag.
4. The release workflow builds and tests the binary, verifies its embedded
   identity/version/checksum, and publishes the binary, manifest,
   `repository.json`, and `SHA256SUMS` as GitHub release assets.
5. Check the latest repository URL and install through Silo's catalog.

The stable repository URL follows GitHub's latest release. Each index pins the
binary URL to a specific tag and SHA-256 checksum, so an index cannot select a
different release's binary. Do not overwrite published assets or reuse tags.

Local packaging uses the same steps:

```sh
docker build --platform linux/amd64 --target artifact --output type=local,dest=plugin/dist plugin
python3 tools/verify_go_bridge.py
python3 tools/package_release.py --tag v0.3.0
(cd plugin/dist && sha256sum -c SHA256SUMS)
```

## Rollback

Download the earlier release's binary and `SHA256SUMS`, verify the binary's
checksum, and upload it through Silo's plugin administration. Open diagnostics
or run the listener task afterward. Back up the Silo plugin configuration before
upgrading; these releases do not migrate or remove settings. The old standalone
Python bridge remains an optional fallback, not a requirement of the plugin.
