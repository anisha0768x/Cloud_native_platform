# Current local audit

This record covers the current source checkout and isolated Docker verification deployments. It is evidence for the tested local workflows, not a claim that every input, race, host failure, or production cloud integration has been proven safe.

## Scope and results

- **API inventory:** the aggregated OpenAPI schema publishes 51 paths and 55 operations. The verification suite sent anonymous requests to every protected published operation and confirmed rejection at the gateway. All schema references resolved.
- **Python regression tests:** 38 passed. These include first-run database readiness, distinct alert observations, roles, sessions, MFA, autoscaling, Kubernetes adapter logic, and forecast query encoding.
- **Real-stack checks:** 20 passed against an isolated deployment with twelve FastAPI services, service-owned PostgreSQL databases, Kafka, Docker workers, SeaweedFS, and Mailpit. The suite exercised authenticated APIs, role and CSRF boundaries, malformed payloads, scaling, incidents, logs, notifications, encrypted storage, backup and full restore, Kafka outage and replay, worker replacement, dependency failure, MFA, and final readiness.
- **Browser workflow:** 22 passed against a fresh isolated deployment. It covered first-run administrator setup, sign-in, all fourteen pages, service and metric forms, scaling and workload execution, byte-exact file upload/download, backup verification, and mobile navigation. No JavaScript exception or HTTP 5xx response was observed in that run.
- **Static gates:** Black, JavaScript syntax checks, `git diff --check`, and `npm ci` completed. The npm dependency audit reported zero vulnerabilities for the installed Node packages. The Python test runtime reported one Starlette test-client deprecation warning.

Local machine-readable evidence is in `test-results/distributed-audit2-rerun.json` and `test-results/browser-v3/report.json`. These files are ignored by Git because they are run-specific. To reproduce the integration suite, use a fresh `helio-verify-*` Compose project; it creates accounts, injects faults, and restores test data.

## Issues corrected

1. **Fresh PostgreSQL startup race:** the launcher now retries service-role login checks while first-run database initialization completes. A genuine credential mismatch still produces the recovery instruction.
2. **False incident counts:** the catalog counts distinct, recent metric observations and resets the breach count after stale data. A single repeated sample cannot satisfy a multi-observation threshold.
3. **Viewer mutation:** viewers can no longer mark the shared notification inbox as read. Gateway, service, and browser controls agree on the operator permission.
4. **Malformed JSON causing server errors:** password changes, notification requests, log analysis, and maintenance outcomes now validate field types and bounds before service logic runs. Bad inputs return 4xx validation responses.
5. **Forecast query composition:** service identifiers are URL encoded before the forecast service calls metric history.
6. **Verification reliability:** Kafka replay is checked separately from alert freshness, and the browser suite can exercise administrator setup from a clean deployment.

## Deployment state and limits

The main local stack is configured for `http://127.0.0.1:8090` because another project occupies port 8080. The local Mailpit inbox uses port 8025. The main stack was left running with all twelve application services healthy after the audit. Administrator setup on that main stack remains a user action requiring the private token in `distributed/.env` and a password of the user's choosing.

External Slack, Gmail/Outlook, and generic webhook delivery were not configured with user accounts and were not verified. No live AWS, Azure, GCP, or Kubernetes cluster deployment, independent penetration test, sustained production load test, or multi-host failover was performed. See [CLOUD_STATUS.md](CLOUD_STATUS.md) for the platform's design limits.
