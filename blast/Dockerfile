# Build recipe for the blast-cron Railway service.
#
# Unlike web and rapsodo-cron, this job needs a real browser: the Blast swing
# data lives inside an embedded AWS QuickSight dashboard with no JSON API, so the
# only way to get the "All swings by player" CSV is to render the page and click
# its export. That means headless Chromium in the container, which is why this
# service has its own Dockerfile instead of riding the default RAILPACK build.
#
# Point the service at this file with RAILWAY_DOCKERFILE_PATH=blast/Dockerfile.
# The build context is the repo root (railway up uploads the whole directory),
# so db.py / metrics.py / blast/ are all present for COPY . .
FROM python:3.12-slim

WORKDIR /app

# playwright install --with-deps pulls the ~30 apt libraries headless Chromium
# needs (libnss3, libatk, fontconfig, ...). Pinned to the version this was
# written and tested against so a future Playwright release can't change the
# click behaviour under an unattended job.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt playwright==1.58.0 \
    && playwright install --with-deps chromium

COPY . .

# --commit: this is the scheduled job, so it writes. DATABASE_URL resolves to
# the internal Postgres inside Railway, exactly as rapsodo-cron's does, so the
# load lands in the database the hub actually reads.
CMD ["python", "blast/pull_export.py", "--commit"]
