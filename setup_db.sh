#!/usr/bin/env bash
# One-off local bootstrap for environments without root/apt:
# installs PostgreSQL + PostGIS into /tmp via micromamba, inits a cluster,
# creates the database/extension and runs migrations.
set -euo pipefail

PG_PREFIX=${PG_PREFIX:-/tmp/pg}
PGDATA=${PGDATA:-/tmp/pgdata}
PGSOCK=${PGSOCK:-/tmp/pgsock}
DB=${PGDATABASE:-sanitation}
USER=${PGUSER:-node}
MM_ROOT=${MM_ROOT:-/tmp/mamba}

export PATH="$PG_PREFIX/bin:$PATH"

if [ ! -x "$PG_PREFIX/bin/postgres" ]; then
  echo "[1/5] installing PostgreSQL + PostGIS via micromamba ..."
  ARCH=$(uname -m); [ "$ARCH" = "x86_64" ] && MM_ARCH=linux-64 || MM_ARCH=linux-aarch64
  curl -Ls "https://micro.mamba.pm/api/micromamba/$MM_ARCH/latest" -o /tmp/mm.tar.bz2
  rm -rf /tmp/mm-bin && mkdir -p /tmp/mm-bin
  tar -xjf /tmp/mm.tar.bz2 -C /tmp/mm-bin bin/micromamba
  MAMBA_ROOT_PREFIX="$MM_ROOT" /tmp/mm-bin/bin/micromamba create -y -p "$PG_PREFIX" -c conda-forge postgresql postgis
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[2/5] initializing cluster at $PGDATA ..."
  rm -rf "$PGDATA" "$PGSOCK" && mkdir -p "$PGDATA" "$PGSOCK"
  initdb -D "$PGDATA" -U "$USER" --auth=trust -E UTF8
fi

echo "[3/5] starting server on unix socket $PGSOCK ..."
pg_ctl -D "$PGDATA" -o "-k $PGSOCK -p 5432 -c listen_addresses='' -c unix_socket_directories=$PGSOCK" -l /tmp/pg.log start -w

if ! psql -h "$PGSOCK" -U "$USER" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1; then
  echo "[4/5] creating database $DB + PostGIS extension ..."
  createdb -h "$PGSOCK" -U "$USER" "$DB"
  psql -h "$PGSOCK" -U "$USER" "$DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;"
fi

echo "[5/5] migrations ..."
export LD_LIBRARY_PATH="$PG_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PROJ_DATA="$PG_PREFIX/share/proj" PROJ_LIB="$PG_PREFIX/share/proj" GDAL_DATA="$PG_PREFIX/share/gdal"
export PGDATABASE="$DB" PGUSER="$USER" PGHOST="$PGSOCK" PGPORT=5432
cd "$(dirname "$0")"
.venv/bin/python manage.py migrate

cat <<EOF

Ready.  Env for new shells:
  export LD_LIBRARY_PATH=$PG_PREFIX/lib:\$LD_LIBRARY_PATH
  export PROJ_DATA=$PG_PREFIX/share/proj PROJ_LIB=$PG_PREFIX/share/proj GDAL_DATA=$PG_PREFIX/share/gdal
  export PGDATABASE=$DB PGUSER=$USER PGHOST=$PGSOCK PGPORT=5432

Run:     .venv/bin/python manage.py runserver 127.0.0.1:8000
Schema:  .venv/bin/python manage.py spectacular --file openapi.yaml
Tests:   MEDIA_ROOT=/tmp/test_media .venv/bin/python manage.py test inspections
Samples: .venv/bin/python manage.py generate_sample_photos
Stop DB: pg_ctl -D $PGDATA stop
EOF
