# db-checker in a container.
#
# This file is here to be READ as much as run. Every line is a decision, and the
# comments say why - because the point of learning Docker is understanding what
# each instruction costs you, not copying a template.
#
#   docker build -t db-checker .
#   docker run --rm -p 8787:8787 db-checker
#
# Then open http://localhost:8787
#
# For the whole thing wired to a throwaway Postgres, use compose instead:
#   docker compose up --build

# ---------------------------------------------------------------- base image
#
# python:3.12-slim, not python:3.12. "slim" is Debian with the build tools and
# docs stripped: ~120MB instead of ~1GB. The full image is only worth it when you
# are compiling C extensions, and this app has no dependencies to compile.
#
# The version is pinned. "python:latest" means your image silently changes under
# you, and a build that worked last month fails today for no visible reason.
FROM python:3.12-slim

# ------------------------------------------------------------- system packages
#
# db-checker drives psql rather than using a database driver, so the client has
# to be in the image. This is the one thing that genuinely must be installed.
#
# Three things happen in ONE RUN on purpose. Each RUN creates a layer, and a
# layer keeps whatever the previous one left behind - so cleaning the apt cache
# in a separate RUN would remove nothing from the image, just add a layer saying
# it was removed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --------------------------------------------------------------- the app files
#
# Copied one by one rather than "COPY . ." so the image holds exactly what it
# needs. .dockerignore backs this up, but being explicit means a stray
# servers.json or .env in the build folder cannot end up baked into an image
# that gets pushed to a registry. Credentials in a layer are forever - deleting
# the file in a later layer does not remove it from the image.
COPY db_checker.py app.py store.py ./
COPY servers.json.example checks.json.example ./

# --------------------------------------------------------------------- runtime
#
# 0.0.0.0, which would be wrong on a laptop and is right here. Inside a container
# 127.0.0.1 means the container itself, so binding there makes the app
# unreachable even from the host. The container is the isolation boundary now.
ENV DBC_HOST=0.0.0.0 \
    DBC_PORT=8787 \
    PSQL_PATH=psql \
    DBC_DB=/data/db-checker.sqlite3 \
    PYTHONUNBUFFERED=1

# PYTHONUNBUFFERED above is not cosmetic: without it Python buffers stdout when
# it is not a terminal, and `docker logs` shows nothing until the buffer flushes
# or the process dies. It is the most common reason a container looks silent.

# Containers are disposable; the accounts, sign-in history and run history are
# not. Anything that must survive `docker rm` lives under a volume.
VOLUME ["/data", "/app/reports"]

# Declares intent only - it does not open anything. Publishing the port is the
# -p flag's job at run time. Worth knowing so you do not spend an hour wondering
# why EXPOSE alone changed nothing.
EXPOSE 8787

# Runs as root by default, which is worth being uncomfortable about. Left as-is
# only because /data and /app/reports are bind-mounted from the host and a
# non-root user hits permission errors on them - a real trade-off, not an
# oversight. In production you would create a user and fix the mount ownership.

CMD ["python", "app.py"]
