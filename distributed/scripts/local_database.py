"""Local PostgreSQL credential recovery. Never delete or recreate data volumes."""

from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

SERVICES = (
    "gateway",
    "auth",
    "catalog",
    "metrics",
    "capacity",
    "logs",
    "forecast",
    "maintenance",
    "notifications",
    "storage",
    "configuration",
    "dashboard",
)


def login_check_script():
    # Values are read inside the container, never interpolated into shell code.
    return """set -eu
export PGCONNECT_TIMEOUT=4
failed=0
for service in postgres %s; do
  if [ "$service" = postgres ]; then
    role=postgres
    password="$POSTGRES_PASSWORD"
  else
    role="helio_$service"
    upper=$(printf '%%s' "$service" | tr '[:lower:]' '[:upper:]')
    eval 'password=${'"${upper}"'_DB_PASSWORD}'
  fi
  if ! PGPASSWORD="$password" psql -h 127.0.0.1 -U "$role" -d "$role" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    printf '%%s\n' "$service"
    failed=1
  fi
done
exit "$failed"
""" % " ".join(SERVICES)


def check_database(compose, child_env):
    result = subprocess.run(
        compose + ["exec", "-T", "postgres", "sh", "-s"],
        # Send LF bytes: Windows text-mode stdin otherwise converts this to CRLF,
        # which Linux /bin/sh rejects (including `set -eu\r`).
        input=login_check_script().encode("utf-8"),
        capture_output=True,
        env=child_env,
        timeout=90,
    )
    if result.returncode:
        names = [
            s
            for s in result.stdout.decode("utf-8", "replace").split()
            if s in ("postgres", *SERVICES)
        ]
        description = ", ".join(names) if names else "PostgreSQL connection check"
        raise SystemExit(
            "Database access failed: " + description + ".\n"
            "If this is your existing local database, run:\n"
            "  python run_local.py --repair-database --no-build --open\n"
            "Recovery backs up the database and aligns its passwords with distributed/.env. "
            "Do not delete data volumes."
        )


def password_sql(values):
    pairs = [("postgres", "POSTGRES_PASSWORD")]
    pairs.extend(("helio_" + name, name.upper() + "_DB_PASSWORD") for name in SERVICES)
    statements = [
        "BEGIN;",
        "SET LOCAL log_statement = 'none';",
        "SET LOCAL log_min_error_statement = 'panic';",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    for role, key in pairs:
        password = values[key]
        if not password or "\x00" in password or "\n" in password or "\r" in password:
            raise SystemExit("Invalid or missing credential: " + key)
        statements.append(
            'ALTER ROLE "' + role + "\" PASSWORD '" + password.replace("'", "''") + "';"
        )
    return "\n".join(statements + ["COMMIT;"])


def repair_database(compose, values, env_file, root, rotate=False):
    child_env = dict(os.environ, **values)
    if rotate and child_env.get("S3_ENDPOINT", "http://objects:8333") != "http://objects:8333":
        raise SystemExit(
            "Access rotation requires the bundled local S3 service. Use --repair-database without --rotate-access-secrets for external S3."
        )

    def run(*args, **kwargs):
        return subprocess.run(compose + list(args), env=child_env, check=True, **kwargs)

    print("Stopping application services for a consistent database backup...", flush=True)
    run("stop", "--timeout", "20", *SERVICES)
    run("up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "postgres")
    result = run(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-Atqc",
        "SELECT rolname FROM pg_roles WHERE rolname LIKE 'helio_%'",
        capture_output=True,
        text=True,
        timeout=20,
    )
    missing = {"helio_" + s for s in SERVICES} - set(result.stdout.split())
    if missing:
        raise SystemExit("Database roles are missing; recovery stopped without changing passwords.")

    backup = root / ".recovery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(env_file, backup / "environment-before.env")
    dump = backup / "postgres-before.sql.gz"
    raw_dump = backup / "postgres-before.sql"
    with raw_dump.open("wb") as output:
        result = subprocess.run(
            compose + ["exec", "-T", "postgres", "pg_dumpall", "-U", "postgres"],
            env=child_env,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    if result.returncode:
        raise SystemExit("Database backup failed; no passwords were changed.")
    with raw_dump.open("rb") as source, gzip.open(dump, "wb") as output:
        shutil.copyfileobj(source, output)
    with gzip.open(dump, "rb") as verify:
        if b"PostgreSQL database cluster dump complete" not in verify.read():
            raise SystemExit("Database backup is incomplete; no passwords were changed.")
    raw_dump.unlink()

    updated = dict(values)
    if rotate:
        for key in ("POSTGRES_PASSWORD", "SETUP_TOKEN", "INTERNAL_SECRET", "S3_SECRET_KEY"):
            updated[key] = secrets.token_urlsafe(48 if key == "INTERNAL_SECRET" else 32)
        for name in SERVICES:
            updated[name.upper() + "_DB_PASSWORD"] = secrets.token_urlsafe(32)
        # Encryption keys deliberately stay unchanged: existing objects/MFA may use them.
    sql = password_sql(updated)
    pending = env_file.with_name(".env.repair-pending")
    if rotate:
        pending.write_text(
            "\n".join(k + "=" + v for k, v in updated.items()) + "\n", encoding="utf-8"
        )
    result = subprocess.run(
        compose
        + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-q",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        text=True,
        capture_output=True,
        env=child_env,
        timeout=30,
    )
    if result.returncode:
        # Do not echo SQL diagnostics: they may contain passwords.
        raise SystemExit(
            "Password repair failed. The SQL transaction rolled back; backup: " + str(backup)
        )
    if rotate:
        try:
            pending.replace(env_file)
        except OSError:
            raise SystemExit(
                "Database credentials changed, but .env could not be replaced. "
                "Rename distributed/.env.repair-pending to .env before starting. Backup: "
                + str(backup)
            )
    (backup / "recovery.json").write_text(
        json.dumps(
            {
                "project": values["COMPOSE_PROJECT_NAME"],
                "database_backup": dump.name,
                "passwords_repaired": 13,
                "access_credentials_rotated": rotate,
                "encryption_keys_preserved": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Database passwords repaired. Private recovery backup: " + str(backup), flush=True)
    return updated
