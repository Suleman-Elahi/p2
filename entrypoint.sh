#!/bin/sh
# Keep files under /storage group/world-readable. Granian serves object bytes
# itself, so this is no longer required by an external nginx, but it keeps the
# volume files readable by tooling and by other services sharing the mount.
umask 022
# Raise per-process file descriptor limit for 8 workers under high concurrency
ulimit -n 65536
set -e
exec "$@"
