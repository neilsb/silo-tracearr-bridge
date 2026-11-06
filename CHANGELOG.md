# Changelog

## 0.3.2

- Recognize TV episode playback when Silo reports the parent type `series`,
  preserving episode titles, series names, and season/episode numbers (including
  season-zero specials).
- Enrich active sessions with year and series identity from targeted, cached
  catalog lookups. Metadata lookup failures never hide active playback.
- Existing history records already stored by Tracearr are not rewritten.

## 0.3.1

- Fix repository manifests to use Silo's numeric form-control enum format.
- Validate each release index using Silo's protobuf types and JSON decoder.
- Use this release for repository installation; 0.3.0's binary works but its
  catalog index cannot be read by Silo.

## 0.3.0

- Standalone Silo plugin repository hosted in GitHub release assets.
- Tagged release workflow with binary, manifest, repository index, and checksums.
- Admin diagnostics page for listener health, upstream checks, library scan
  status, artwork failures, and connection troubleshooting.
- MIT license and documented installation, upgrades, and rollback.

## 0.2.5

- Pace artwork metadata and catalog requests together; retry rate-limited reads.
- Coalesce concurrent requests for the same poster.

## 0.2.4

- Share catalog scans and retain progress across caller timeouts.
- Retry rate-limited pages and publish complete snapshots only.

## 0.2.3

- Resolve library artwork without active playback, including after restarts.

## 0.2.2

- Richer video dimensions, audio layout, and hardware encoding details.

## 0.2.1

- Listener diagnostics and periodic recovery scheduling support.

## 0.2.0

- Native Silo plugin with playback monitoring, catalog sync, posters, and
  authority-revoking stream termination.
