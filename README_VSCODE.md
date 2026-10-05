# Run Helio Operations in VS Code

Open the `Helio_Cloud_Operations` folder in VS Code. Keep Docker Desktop open with its Linux engine running. Open **Terminal > New Terminal**. Use Python 3.11 or later.

## Start the website

```powershell
python run_local.py --open
```

This builds the images, checks PostgreSQL access, starts the twelve services and waits for readiness. The website is at http://127.0.0.1:8080. If administrator setup is required, use the private token printed in this terminal and choose your own email/password. Do not post the token or `.env` in chat.

After the images have been built, start faster with:

```powershell
python run_local.py --no-build --open
```

## Stop without deleting data

```powershell
python run_local.py --stop
```

## Recover a database password mismatch

Use this only when the launcher reports database access failures:

```powershell
python run_local.py --repair-database --no-build --open
```

Recovery backs up `.env` and all PostgreSQL databases in the private `.recovery/` folder before updating passwords. It preserves database and object-storage volumes. It never deletes users or files. Never delete data volumes or regenerate encryption keys as a troubleshooting shortcut.

If Docker reports another unhealthy service, inspect its status and logs:

```powershell
docker compose --env-file distributed/.env -f distributed/compose.yaml ps
docker compose --env-file distributed/.env -f distributed/compose.yaml logs --tail 60 gateway
```

Replace `gateway` with the failing service name. Do not share logs containing secrets.

The browser interface is in `web/`; backend modules are in `distributed/helio/services/`. After code edits, use the normal launcher without `--no-build`. All launchers (`run_local.py`, `app.py`, `launch_platform.py` and the Windows `.cmd`) use the same backend. Local test emails appear at http://127.0.0.1:8025.

See `docs/STARTUP_REPAIR_2026-10-05.md` for the current repair verification. Previous integration results in `docs/VERIFICATION.md` describe the earlier release; `docs/CLOUD_STATUS.md` describes remaining cloud limitations.
