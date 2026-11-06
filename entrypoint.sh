#!/bin/sh
# Docker creates a missing bind-mount folder as root. When started as root, take
# ownership of the data folder, then run the service as the unprivileged user.
set -e
if [ "$(id -u)" = 0 ]; then
  chown -R app:app "${DATA_DIR:-/data}"
  exec setpriv --reuid=app --regid=app --init-groups python /app/service.py
fi
exec python /app/service.py
