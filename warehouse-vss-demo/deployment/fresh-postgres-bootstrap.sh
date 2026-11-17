#!/bin/sh
# Fresh-install path only. No retained-database rename or data removal.
set -eu
umask 077
pgdata=/var/lib/postgresql/data/pgdata
socket=/var/run/postgresql
owner=/var/lib/postgresql/data/.warehouse-fresh-bootstrap-v1
complete=/var/lib/postgresql/data/.warehouse-fresh-bootstrap-complete-v1
test -n "${PG_ADMIN_PASSWORD:?existing admin Secret is required}"
test -n "${PG_APP_PASSWORD:?existing application Secret is required}"
mkdir -p "$socket"
running=false
cleanup() {
  if test "$running" = true; then pg_ctl -D "$pgdata" -w stop -m fast >/dev/null 2>&1 || true; fi
  if test -f "$socket/bootstrap-admin-password"; then rm -f "$socket/bootstrap-admin-password"; fi
}
trap cleanup EXIT HUP INT TERM

if test -s "$pgdata/PG_VERSION"; then
  test -f "$owner" || { echo 'Refusing an unowned preexisting PGDATA; no data changed.' >&2; exit 1; }
else
  mkdir -p "$pgdata"
  test -z "$(find "$pgdata" -mindepth 1 -maxdepth 1 -print -quit)" || {
    echo 'Fresh PGDATA is not empty; inspect the retained partial initialization.' >&2; exit 1;
  }
  touch "$owner"
  printf '%s\n' "$PG_ADMIN_PASSWORD" > "$socket/bootstrap-admin-password"
  initdb -D "$pgdata" --username=postgres --auth-local=trust --auth-host=scram-sha-256 \
    --pwfile="$socket/bootstrap-admin-password" >/dev/null
  rm -f "$socket/bootstrap-admin-password"
fi

pg_ctl -D "$pgdata" -w start \
  -o "-c listen_addresses='' -c unix_socket_directories=$socket -c password_encryption=scram-sha-256" >/dev/null
running=true
PGHOST="$socket" PGUSER=postgres PGDATABASE=postgres psql --no-psqlrc -q --set=ON_ERROR_STOP=1 <<'SQL'
\getenv admin_password PG_ADMIN_PASSWORD
\getenv app_password PG_APP_PASSWORD
SELECT 'CREATE ROLE vst LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vst') \gexec
SELECT format('ALTER ROLE vst PASSWORD %L', :'app_password') \gexec
SELECT format('ALTER ROLE postgres PASSWORD %L', :'admin_password') \gexec
SELECT 'CREATE DATABASE nvcentralizedb OWNER vst'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'nvcentralizedb') \gexec
ALTER DATABASE nvcentralizedb OWNER TO vst;
REVOKE ALL ON DATABASE nvcentralizedb FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE nvcentralizedb TO vst;
SQL
PGHOST="$socket" PGUSER=postgres PGDATABASE=nvcentralizedb psql --no-psqlrc -q --set=ON_ERROR_STOP=1 <<'SQL'
ALTER SCHEMA public OWNER TO vst;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO vst;
SQL
pg_ctl -D "$pgdata" -w stop -m fast >/dev/null
running=false
touch "$complete"
trap - EXIT HUP INT TERM
echo 'Fresh Warehouse database and application role initialized; credentials suppressed.'
