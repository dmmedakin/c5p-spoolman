FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/dmmedakin/c5p-spoolman" \
      org.opencontainers.image.description="Charges Spoolman spools with the filament a FlashForge Creator 5 Pro really uses" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8000

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && useradd --no-create-home --uid 10001 --user-group app \
 && mkdir -p /data && chown app /data
COPY --chmod=755 entrypoint.sh /app/entrypoint.sh
COPY service.py tracker.py allocate.py gcode_usage.py /app/

# Starts as root only to take ownership of /data, then runs as "app" (entrypoint.sh)
WORKDIR /app
VOLUME /data
EXPOSE 8000

# Healthy while the poll loop runs; the printer being off is normal and does not count.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=5)"

ENTRYPOINT ["/app/entrypoint.sh"]
