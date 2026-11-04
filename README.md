# Silo–Tracearr bridge

A standalone, read-only service that presents Silo's native monitoring API as
the Jellyfin API consumed by Tracearr. Written in Python 3.12+, with no runtime
dependencies. This is an independent integration, not an official Silo plugin.

Implementation is in progress. The initial target is server connection, users,
live playback polling, and movie/TV library synchronization. SSE and playback
control are outside the initial version.
