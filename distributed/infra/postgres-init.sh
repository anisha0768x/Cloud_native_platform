#!/bin/sh
set -eu
for service in gateway auth catalog metrics capacity logs forecast maintenance notifications storage configuration dashboard; do
  upper=$(printf '%s' "$service" | tr '[:lower:]' '[:upper:]')
  eval "password=\${${upper}_DB_PASSWORD}"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres --set=role="helio_$service" --set=secret="$password" <<'SQL'
CREATE ROLE :"role" LOGIN PASSWORD :'secret';
CREATE DATABASE :"role" OWNER :"role";
REVOKE CONNECT ON DATABASE :"role" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"role" TO :"role";
SQL
done
