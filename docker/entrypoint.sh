#!/bin/bash
# Runs as root only long enough to make /data writable for the "dwm" user, then drops privileges.
# The Dragonwilds server refuses to run as root, so the manager and the servers run as dwm.
set -e
if [ "$(id -u)" = "0" ]; then
  if [ "$(stat -c %u /data)" != "$(id -u dwm)" ]; then
    chown dwm:dwm /data
  fi
  # files written by an earlier root-run container or a bind mount, if any (only touches foreign files)
  find /data -not -user dwm -exec chown dwm:dwm {} + 2>/dev/null || true
  exec setpriv --reuid=dwm --regid=dwm --init-groups /usr/bin/tini -- "$@"
fi
exec /usr/bin/tini -- "$@"
