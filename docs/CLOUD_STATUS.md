# Honest cloud-computing status — verified local release

This is a working distributed cloud-operations project with a verified local runtime and an offline-validated AWS deployment implementation. It is not yet a live-production-certified AWS installation.

## Completed and verified

- Twelve running FastAPI microservices behind an API gateway.
- A separate PostgreSQL database and login for each service; cross-service database access was denied in verification.
- Kafka event delivery with persistent outboxes, deduplicating consumers, retries and failed-event replay.
- Real authentication, roles, CSRF protection, throttling, expiring sessions and authenticator MFA.
- Measured request/worker monitoring and evidence-based incident workflows.
- Actual Docker workload scaling, measured-demand autoscaling and failed-worker replacement.
- Encrypted/versioned S3-compatible object storage with byte checks and application retention.
- Persistent notifications and verified local SMTP acceptance through Mailpit.
- Encrypted backups, restoration checks and exercised restoration of records/files.
- Chronologically evaluated statistical forecasts, partial cost estimates and measured service-level ratios.
- Readable responsive frontend with Team/Group 11 branding removed.
- Terraform deployment for private ECS Fargate services, ALB, RDS Multi-AZ, MSK Serverless, S3, SES/IAM, Secrets Manager, CloudWatch and immutable ECR images.
- ECS workload scaling adapter, IAM-based MSK authentication, IAM-based S3 access and native SES delivery.
- PostgreSQL advisory locking for replica-safe scheduled work and a two-stage database/topic bootstrap procedure.

## Incomplete or limited

- AWS resources have not been created in a real account, so live IAM, failover, load, restore, SES deliverability and cloud billing remain unverified.
- Kubernetes adapter logic and focused checks exist, but no live cluster deployment was verified.
- The stack runs on one computer. Worker replacement does not survive loss of that machine.
- Predictive-maintenance observations/labels are collected, but no trained failure-prediction model is deployed.
- Log diagnosis uses transparent rules rather than a GenAI model. Forecasts do not drive autoscaling.
- External Gmail, Outlook, Slack or generic webhook delivery was not verified with user accounts.
- AWS ingress is designed for ALB TLS, while internal Service Connect traffic still uses HTTP plus the shared internal application secret rather than mTLS.
- The capacity service controls Docker through the Docker socket, which is a deliberate local-lab privilege.
- Backups remain in the same local object-store installation. Multi-service restore is coordinated rather than atomic.
- Cost figures are partial estimates, not provider bills or proof of savings.
- The frontend is vanilla JavaScript. TimescaleDB, OpenSearch and Redis are not deployed.

The defensible description is: **a local cloud-operations platform demonstrating microservices, event-driven communication, service-owned databases, container elasticity, monitoring, access control and recovery.**
