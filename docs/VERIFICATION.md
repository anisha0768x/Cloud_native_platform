# Helio Operations v3 — verification record

This is the earlier release record. See [CURRENT_AUDIT.md](CURRENT_AUDIT.md) for the latest local audit and fixes.

This record describes the distributed release, not the earlier single-process prototype. Checks were executed locally on Windows with Docker Desktop's Linux engine, Python 3.13 and Microsoft Edge through Playwright. The real-stack integration report is dated **30 September 2026**. Final browser and source-package checks passed on **1 October 2026** and are recorded below.

## Executed checks

| Check | Result | What it establishes |
|---|---|---|
| Focused Python regression suite | **13 passed** | Session/MFA enforcement, roles, CSRF, setup authorization, Docker readiness, Kubernetes verification logic, autoscaler bounds/cooldown, worker replacement and chronological forecast evaluation. |
| Real-stack integration and fault suite | **18 passed** | Actual PostgreSQL, Kafka, Docker workers, S3 storage and SMTP operations through twelve running service containers. The detailed checks are listed below. |
| Final browser workflow | **21 passed**; `verification/browser.json` | Checks all fourteen pages, authenticated forms, actual scaling/workloads, byte-exact upload/download, backup verification and a 390-pixel mobile viewport. |
| Source archive and fresh extraction | **8 passed**; `verification/package.json` | Records archive hash validation and startup from a clean extracted source folder with a separate fresh deployment. |

These counts describe different test levels; they are not a percentage of correctness or a production certification. The unit run produced one dependency deprecation warning in Starlette's HTTP test client. The GitHub Actions workflow is included, but no remote CI run is claimed.

Sanitized machine-readable reports are in `docs/verification/`. Local screenshots, operational test data, generated environment files and credentials are excluded from the source release.

## The eighteen real-stack checks

1. First-administrator setup and authenticated requests through the gateway.
2. Twelve separately running services and denial of cross-service PostgreSQL database access.
3. Server-side roles, CSRF enforcement and last-administrator protection.
4. Real worker scaling, HTTP workload execution and Kafka-delivered measurements/logs.
5. Unknown service registration, fresh metric observations and the incident lifecycle with recovery evidence.
6. Diagnostic results linked to stored evidence and redaction of known credential patterns.
7. Asynchronous acceptance by a real local SMTP receiver and duplicate suppression.
8. Actual S3 bytes, encryption, versions, retention enforcement and verified download content.
9. Cost arithmetic plus truthful forecast/maintenance availability and limitations.
10. Automatic scaling based on measured demand with bounds and cooldown.
11. Detection of a stopped worker and replacement by a different healthy container.
12. Kafka outage: pending outbox events survive, resume after recovery and do not create duplicate incidents after a consumer restart.
13. A stopped forecast service produces an unavailable response; unrelated operations still work and the service recovers.
14. A deliberately rejected event reaches the failed-event queue. Replay fails while the cause remains, then processes exactly once after the cause is repaired.
15. SMTP outage: delivery retries exhaust without stopping metric collection; a later retry succeeds after the receiver returns.
16. Encrypted backup, temporary PostgreSQL restoration checks, actual full restore, removal of post-backup records, file recovery and session revocation.
17. MFA enrollment revokes older sessions; an unverified privileged session cannot bypass required MFA.
18. Final readiness of all twelve services, persisted audit events and the API specification.

Faults were injected only into isolated `helio-verify-*` deployments. These tests intentionally stop test containers and restore test data. Do not run the integration suite against your demonstration or personal data. Use a fresh verification deployment for a complete rerun; the suite creates users and changes persistent state.

## The seven issue areas

| Issue | What changed | Remaining boundary |
|---|---|---|
| Broken actions and inconsistent values | Forms use validated APIs; scale outcomes reflect application readiness; costs use explicit rates and a common monthly basis; failures are visible. | Tests cover the documented workflows, not every possible input or race. |
| Unsupported claims | Fixed health/accuracy/delivery claims were removed. Evidence, method, freshness and configuration status are shown. | Forecasting, maintenance and cost limitations remain visible rather than being represented as completed AI/cloud services. |
| Disconnected monitoring | Actual HTTP requests and submitted observations travel through Kafka into persisted measurements, logs and incident evaluation. | No automatic discovery of every workload in a cloud account. |
| Incomplete incident handling | Ownership, acknowledgement, investigation, notes, recovery evidence, resolution and audit history are enforced. | No full on-call scheduling/escalation product. |
| Authentication and permissions | Real accounts, hashed passwords/sessions, expiry/revocation, RBAC, CSRF, throttling and per-session verified TOTP MFA. | No SSO, recovery codes or independent penetration test. |
| Frontend usability | Readable light surfaces, restrained colors, responsive navigation, clear forms, timestamps and empty/error states; Team/Group 11 branding removed. | Formal accessibility certification was not performed. |
| Deployment/backend mismatch | Twelve real services, private databases, Kafka delivery, Docker scaling, encrypted S3 files, local SMTP and restoration replace earlier simulated/monolithic claims. | Single host, no infrastructure HA, no live public-cloud or Kubernetes validation. |

## What these checks do not establish

- **Kubernetes was tested through focused adapter tests, not a live cluster.** No configured cluster context was available.
- **No public AWS/Azure/GCP deployment or managed cloud integration was verified.** The object store is local S3-compatible SeaweedFS.
- PostgreSQL, Kafka and S3 each run on one host. Container recovery does not prove resilience to host or region failure.
- Local SMTP acceptance is not delivery to Gmail/Outlook. External SMTP, Slack and webhook accounts remain unconfigured and unverified.
- Forecasts use simple evaluated statistical models. Predictive maintenance has evidence collection but no trained model. Log diagnosis uses rules, not a hosted LLM.
- Default traffic uses loopback/bridge HTTP and a private shared-secret boundary, not TLS/mTLS. The Docker capacity controller has privileged access through the Docker socket.
- Backups live on the same installation. Multi-service restore is coordinated, not an atomic transaction or off-site recovery guarantee.
- No sustained production load test, independent security audit or certified RPO/RTO/SLA has been completed.

See [CLOUD_STATUS.md](CLOUD_STATUS.md) for the complete implementation/limitation matrix. The microservices and tested local workflows are real. Calling all original enterprise-cloud commitments fulfilled, or the software flawless, would be inaccurate.

## Reproduce and package

Follow the root [README](../README.md) for setup and the unit, integration and browser commands. The Docker images and Python dependencies are pinned. Node.js is needed only for browser tests; the normal launcher uses host Python's standard library.

From the project root, create the source release with:

```powershell
python distributed/scripts/package_release.py
```

The output is `output/Source/Helio_Cloud_Operations_Audited.zip`, with a separate `.zip.sha256` checksum. `SOURCE_MANIFEST.json` records the SHA-256 of every included source/document file. The packager checks each archived hash, ZIP integrity and generated-secret exclusion. It excludes earlier prototypes, operational data, `.env` files, keys, dependencies and test screenshots.
