# Startup repair and current verification — 5 October 2026

The existing local deployment in `Helio_Cloud_Operations` has been repaired and tested. Open the project folder in VS Code, keep Docker Desktop running, and run:

```powershell
python run_local.py --no-build --open
```

Website: http://127.0.0.1:8080. Local test email inbox: http://127.0.0.1:8025.

The main installation had no administrator account before recovery. The setup page is expected: use the **new** setup token printed by the launcher and choose your own name, email and password. The token previously pasted in chat is no longer valid. Test accounts exist only in the separate verification deployment on port 8090, which was stopped after verification.

## Confirmed causes and changes

1. Existing PostgreSQL roles rejected the service credentials in `.env`, preventing all twelve application services from starting. A database dump and original configuration were saved before reconciling the 13 login passwords (PostgreSQL administrator plus 12 service roles). No database or object volume was deleted.
2. An unhealthy gateway still occupied port 8080. The launcher tried to read its HTTP response, raised `RemoteDisconnected`, and never reached recovery. It now recognizes its own running/restarting gateway using Docker ownership and port bindings; other applications remain protected.
3. The supported wrapper called `main()` directly and bypassed the startup error handler. All entry points now use the error-handling wrapper.
4. The deployment retained folder labels from two earlier checkouts. The launcher now checks the live gateway/database location while allowing unchanged infrastructure labels from an earlier location. Another live installation is not silently taken over.
5. Database logins are checked before the full application startup. A mismatch gives an explicit recovery command instead of waiting for repeated health-check failures.
6. The recovery check sends Linux shell input as LF bytes, avoiding Windows CRLF conversion.

## Recovery and credential handling

`python run_local.py --repair-database --no-build --open` creates a private backup, updates existing role passwords in one transaction, and starts the stack. It does not reset application data. Do not delete volumes or regenerate `.env` while keeping an existing database.

For this repair, exposed PostgreSQL/service passwords, internal access secret, setup token and local S3 secret were replaced. The previous notifications database password and S3 secret were explicitly tested and rejected. Authentication/storage encryption keys were preserved; rotating them requires a planned migration for any encrypted data. Treat the private `.env` and `.recovery/` files as sensitive. They are excluded from Git, Docker builds and the source ZIP.

## Checks performed in this repair session

| Check | Result |
| --- | --- |
| Existing backend regression suite | 13 passed |
| New launcher/recovery regressions | 12 passed |
| Real backend integration scenarios | 12 passed in an isolated deployment |
| Main installation readiness | All 12 services returned `ready` |
| Normal build/start command | Passed |
| Stop followed by restart without rebuilding | Passed; data volumes retained |
| Website and administrator setup endpoint | HTTP 200; setup required as expected |
| Browser navigation | All 14 sections loaded with no observed JavaScript errors |
| Browser sign-in/sign-out | Passed using an isolated test account |
| Browser scaling | Actual workers changed from 2 to 1; readiness verified |
| Browser workload test | 20 of 20 requests completed, 0 errors |

The backend integration checks covered administrator setup, sessions, role restrictions, CSRF protection, service database isolation, actual Docker scaling, Kafka telemetry, incident transitions, evidence-linked log analysis, local SMTP, encrypted/versioned S3 files, retention, cost arithmetic, forecasting limits, backup verification, actual restoration and MFA.

Machine-readable reports:

- `verification/startup-repair-integration-20261005.json`
- `verification/recovered-runtime-20261005.json`
- `verification/startup-repair-browser-20261005.json`

The earlier fault-injection suite was not repeated during this repair. The results above establish that the listed workflows worked on this machine; they are not a guarantee that no other bugs exist.

## Scope

This remains the verified local Docker release. This repair does not add a public-cloud deployment, live Kubernetes verification, trained failure prediction, Ollama/GenAI diagnosis, cross-host availability, off-site disaster recovery or externally verified Gmail/Outlook/Slack delivery. See `CLOUD_STATUS.md` for the full limitations.
