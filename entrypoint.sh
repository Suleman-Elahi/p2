#!/bin/sh
# Keep files under /storage group/world-readable so the volume files stay
# accessible to tooling and other services sharing the mount.
umask 022
# Raise per-process file descriptor limit for 8 workers under high concurrency
ulimit -n 65536
set -e
exec "$@"
