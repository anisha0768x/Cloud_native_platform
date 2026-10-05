# Cloud Native Platform (Helio Operations)

A cloud-native operations platform built from **twelve independent FastAPI microservices**. It lets teams monitor services, manage incidents, scale workloads, store files securely, send notifications and back up or restore the whole system, all from one web dashboard that runs locally with Docker.

> **Status:** Verified local release. It is a single-host demonstration deployment, not a production-certified system. See the [current audit](docs/CURRENT_AUDIT.md) and [`docs/CLOUD_STATUS.md`](docs/CLOUD_STATUS.md) for checks and limitations.

---

## What the Website Does

After you start the platform, the web dashboard (served at `http://127.0.0.1:8080`) provides:

| Feature | Description |
|---|---|
| **Identity & Access** | First-run administrator setup, role-based access (admin / operator / viewer), expiring and revocable sessions, CSRF protection, login throttling and TOTP multi-factor authentication. |
| **Monitoring (Command Center)** | Live latency, response status, request rate, worker readiness and gateway measurements. |
| **Incident Management** | Threshold-based incident creation, ownership, acknowledgement, investigation notes, and resolution only after fresh recovery evidence. |
| **Capacity & Autoscaling** | Creates and removes real Docker worker containers, verifies readiness, runs bounded HTTP load tests, autoscales on measured demand and replaces failed workers. |
| **Log Intelligence** | Persisted, searchable logs with secret/email redaction and findings linked to evidence IDs. |
| **File Storage** | Encrypted S3-compatible object storage with checksums, versioning and retention protection. |
| **Notifications** | Persistent inbox plus email via a local SMTP server (Mailpit) with retry and duplicate suppression. |
| **Backup & Restore** | Encrypted service snapshots and file archives, checksum verification, full restore (requires backup ID and admin password). |
| **Forecasting** | Simple chronological statistical forecasting with held-out error (MAE) and baseline reporting. |
| **Costs & SLOs** | Partial cost estimates and measured request success/latency ratios. |
| **Audit Trail** | Record of who did what across the platform. |

---

## Architecture

```text
Browser → API Gateway → 12 independent microservices
            ├─ service-owned PostgreSQL databases
            ├─ transactional outbox → Kafka → deduplicating consumers
            ├─ capacity controller → Docker workload containers
            ├─ storage service → encrypted SeaweedFS (S3-compatible) objects
            └─ notification worker → Mailpit (local SMTP)
```

**Key design points**

- **Database per service:** each microservice owns its own PostgreSQL database and login. Services share data only through APIs and Kafka events, never by reading each other's tables.
- **Event-driven messaging:** transactional outbox pattern, Kafka topics, consumer deduplication, bounded retries, and failed-event inspection and replay.
- **Single image, many services:** the same application image is started with twelve different service modules and credentials.
- **Local single-host deployment:** one PostgreSQL server, one Kafka broker and one SeaweedFS installation. Only ports `8080` (dashboard) and `8025` (mail inbox) are published, on loopback only.

See [`ARCHITECTURE_ALIGNMENT.md`](ARCHITECTURE_ALIGNMENT.md) for more detail.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend services | Python 3.11+, FastAPI |
| Databases | PostgreSQL (one database and login per service) |
| Messaging | Apache Kafka |
| Object storage | SeaweedFS (S3-compatible), with application-level encryption |
| Email (local) | Mailpit (SMTP) |
| Containers | Docker, Docker Compose |
| Frontend | Browser web interface (`web/`) |
| CI | GitHub Actions (`.github/workflows`) |
| Security | PBKDF2 password hashing, TOTP MFA, CSRF protection, role-based access |

---

## Getting Started

### Prerequisites

- **Docker Desktop** with the Linux engine running
- **Python 3.11 or later** (for the launcher)
- Several GB of free disk space and enough memory for the containers

### Run the platform

```powershell
# 1. Clone the repository
git clone https://github.com/anisha0768x/Cloud_native_platform.git
cd Cloud_native_platform

# 2. Start Docker Desktop and wait for "Engine running"

# 3. Build and launch everything (first run downloads images; this takes a while)
python run_local.py --open
```

Then open:

- **Dashboard:** http://127.0.0.1:8080
- **Local mail inbox (Mailpit):** http://127.0.0.1:8025

The terminal prints a **one-time setup token**. Use it to create the first administrator account (it is not a login password). To show it again:

```powershell
python distributed/scripts/setup.py --show-setup-token
```

### Everyday commands

| Task | Command |
|---|---|
| Start without rebuilding | `python run_local.py --no-build --open` |
| Stop (keeps your data) | `python run_local.py --stop` |
| Repair database logins | `python run_local.py --repair-database --no-build --open` |
| Repair and rotate access secrets | add `--rotate-access-secrets` to the repair command |
| Run launcher tests | `python -m unittest discover -s distributed/tests -p test_startup.py` |

> ⚠️ Never run `docker compose down -v` to fix a login problem, because it deletes your data volumes.

---

## Suggested Demo Walkthrough

1. Create administrator, operator and viewer accounts and confirm roles block unauthorized actions.
2. Run a bounded workload with one worker, then scale to two and view the replica history.
3. Explore **Command Center**, **Services** and **Log Intelligence**.
4. Enable measured-demand autoscaling and review the action history.
5. Acknowledge, investigate and resolve an incident.
6. Send an email notification and open it in Mailpit.
7. Upload, version, download and checksum a file; test retention protection.
8. Create and verify a backup (full restore requires the backup ID and admin password).
9. Review **Connections** and **Audit Trail**.

---

## Project Structure

```text
.
├── web/                    Browser interface and offline API reference
├── distributed/
│   ├── helio/              Shared transport + the 12 service modules
│   ├── infra/              PostgreSQL database/role initialization
│   ├── scripts/            Secret generation and Docker launcher
│   └── tests/              Regression, integration and browser checks
├── services/workload/      Managed CPU-bound HTTP workload
├── docs/                   Status, verification and architecture docs
├── .github/workflows/      CI workflows
├── run_local.py            Supported launcher
├── Dockerfile
└── docker-compose.yml
```

---

## Verification

| Test suite | Result |
|---|---|
| Focused regression checks | 38 passed |
| Real-stack integration and fault checks | 20 passed |
| Browser workflow checks | 22 passed |
| Clean-source archive/startup checks | 8 passed |

These results cover the tested workflows only and are **not** a production certification. Current details: [`docs/CURRENT_AUDIT.md`](docs/CURRENT_AUDIT.md). Earlier release evidence: [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

---

## Security Notes

- The generated `.env` file contains secrets. **Keep it private and never commit it.**
- Keep `.env` together with its matching data volumes.
- Keep the `.recovery/` folder private. It contains secrets and database records.
- Do not change encryption keys while keeping old encrypted objects or MFA secrets.

---

## Limitations

- Single-host local deployment (one PostgreSQL, one Kafka broker, one SeaweedFS).
- Email is accepted by a local SMTP server (Mailpit) and is not delivered to real inboxes.
- Cost figures are partial estimates.
- Not production-certified. Read [`docs/CLOUD_STATUS.md`](docs/CLOUD_STATUS.md) before presenting the project.

