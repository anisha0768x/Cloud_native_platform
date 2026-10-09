# AWS production deployment

This stack deploys Helio to ECS Fargate in two Availability Zones. It provisions an ALB, private application tasks, RDS PostgreSQL, MSK Serverless, S3, Secrets Manager, CloudWatch, Service Connect and an independently scalable workload service.

## Safety model

The first apply must use `bootstrap_complete=false`. This creates infrastructure and the one-shot bootstrap task, but does not start application services. Run the bootstrap task successfully, then apply with `bootstrap_complete=true`. RDS deletion protection and a final snapshot are enabled by default.

Terraform state must be stored in a separately created, versioned S3 bucket. The bucket is intentionally outside this stack so destroying the application cannot destroy its own state. Restrict access to the deployment role and enable an account-level AWS Budget before applying.

## Required GitHub production variables

- `AWS_DEPLOY_ROLE_ARN`: OIDC deployment role; do not store AWS access keys in GitHub.
- `AWS_REGION`: for example `ap-south-1`.
- `AWS_STACK_NAME`: globally distinguishable stack prefix.
- `TF_STATE_BUCKET`: existing private and versioned Terraform state bucket.
- `AWS_DOMAIN_NAME` and `AWS_CERTIFICATE_ARN`: required before enabling services; the ACM certificate must be in the same region.
- `AWS_ROUTE53_ZONE_ID`: optional existing hosted zone. When supplied, Terraform creates the ALB alias record.
- `AWS_SES_FROM` and `AWS_SES_TO`: verified SES identities. SES must have production access to send to unverified recipients.

Protect the GitHub `production` environment with required reviewers. The workflow only runs after a human types `DEPLOY`.

## First deployment

1. Run the workflow with `bootstrap_complete=false`. It creates the infrastructure, builds immutable images, pushes them to ECR and runs the database/topic bootstrap task.
2. Inspect `/ecs/<stack>/bootstrap` in CloudWatch and confirm the task exited with code zero.
3. Run the workflow again for the same commit with `bootstrap_complete=true`. It starts the application and waits for all ECS services to stabilize.
4. Retrieve the first-administrator setup token from the `/<stack>/auth` Secrets Manager secret. Never copy it into logs or source control.
5. Run the API and browser verification suite against the ALB/domain before switching production DNS.

## Local validation

Run `terraform fmt -check -recursive deploy/aws`, `terraform init -backend=false deploy/aws`, and `terraform validate deploy/aws`. The ordinary Docker Compose deployment remains supported because AWS behavior is selected only by environment variables.

## Known operational decisions

- ECS controls gateway CPU scaling. The Helio capacity controller controls only the separate workload ECS service.
- Singleton collection and delivery jobs use PostgreSQL transaction advisory locks, so extra service replicas do not execute the same scheduled pass concurrently.
- Application services authenticate to MSK Serverless with their ECS task role and to S3/SES through IAM; no static AWS keys are supplied to containers.
- Service database pools default to four connections per task in AWS. Recalculate the RDS connection budget before raising replica limits.
- MSK, NAT Gateway, Multi-AZ RDS and continuously running Fargate tasks incur cost even when idle.
