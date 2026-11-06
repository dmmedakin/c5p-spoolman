#!/bin/sh
# Docker creates a missing bind-mount folder as root. When started as root, take
# ownership of the data folder, then run the service as the unprivileged user.
set -e
DATA="${DATA_DIR:-/data}"
WEBUI="${FLASHFORGE_WEBUI_DIR:-/import/flashforge-webui}"
if [ "$(id -u)" = 0 ]; then
  # FlashForge WebUI may have written its files readable by root only: copy the
  # JSON ones where the service can read them (credentials.py).
  if [ -d "$WEBUI" ]; then
    mkdir -p "$DATA/import"
    find "$WEBUI" -maxdepth 1 -name '*.json' -exec cp {} "$DATA/import/" \; || true
  fi
  chown -R app:app "$DATA"
  exec setpriv --reuid=app --regid=app --init-groups python /app/service.py
fi
exec python /app/service.py
