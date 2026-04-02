#!/bin/sh
# Ensure files written to /storage are world-readable so host Nginx can serve them via X-Accel-Redirect
umask 022
# Raise per-process file descriptor limit for 8 workers under high concurrency
ulimit -n 65536
set -e
exec "$@"
