FROM python:3.12-slim

# No apt-get here on purpose: this host resolves deb.debian.org to IPv6 only
# and Docker's bridge has no IPv6 egress, so any apt step fails the build.
# Everything needed comes from PyPI or the stdlib.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RALLY_DB=/data/rally.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged. /data is a volume mount point, so it must be owned by the
# runtime user before the volume is created or SQLite cannot write there.
RUN useradd --system --uid 10001 --create-home --home-dir /home/rally rally \
    && mkdir -p /data \
    && chown -R rally:rally /data /app

USER rally

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
