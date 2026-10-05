"""Generate unique local secrets and the Compose deployment; never overwrite secrets."""

import argparse
import base64
import json
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
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


def configure(path=None, project="helio-v3", port="8080", mail_port="8025"):
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        values = {
            "COMPOSE_PROJECT_NAME": project,
            "HELIO_PORT": str(port),
            "MAIL_PORT": str(mail_port),
            "INTERNAL_SECRET": secrets.token_urlsafe(48),
            "SETUP_TOKEN": secrets.token_urlsafe(24),
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "S3_ACCESS_KEY": "helio-local",
            "S3_SECRET_KEY": secrets.token_urlsafe(32),
            "AUTH_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "STORAGE_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        }
        values.update({name.upper() + "_DB_PASSWORD": secrets.token_urlsafe(32) for name in NAMES})
        with path.open("x", encoding="utf-8") as file:
            file.write("\n".join(k + "=" + v for k, v in values.items()) + "\n")
        path.chmod(0o600)
    return path


def compose():
    services = {}
    pg_env = {"POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}", "POSTGRES_DB": "postgres"} | {
        name.upper() + "_DB_PASSWORD": "${" + name.upper() + "_DB_PASSWORD}" for name in NAMES
    }
    services["postgres"] = {
        "image": "postgres:17.6@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929",
        "environment": pg_env,
        "volumes": [
            "postgres-data:/var/lib/postgresql/data",
            "./infra/postgres-init.sh:/docker-entrypoint-initdb.d/01-services.sh:ro",
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U postgres"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 20,
        },
        "networks": ["backend"],
        "restart": "unless-stopped",
    }
    services["kafka-volume"] = {
        "image": "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e",
        "user": "0:0",
        "command": ["sh", "-c", "chown -R 1000:1000 /data"],
        "volumes": ["kafka-data:/data"],
        "networks": ["backend"],
        "restart": "no",
    }
    services["kafka"] = {
        "image": "apache/kafka:4.0.0@sha256:3f7b939115cd4872e9cee9369d80bd69712fde55f9902f46d793f64848dedc75",
        "environment": {
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
            "KAFKA_NUM_PARTITIONS": "3",
            "KAFKA_LOG_RETENTION_HOURS": "168",
            "KAFKA_LOG_DIRS": "/tmp/kraft-combined-logs",
            "KAFKA_HEAP_OPTS": "-Xms256M -Xmx512M",
        },
        "volumes": ["kafka-data:/tmp/kraft-combined-logs"],
        "depends_on": {"kafka-volume": {"condition": "service_completed_successfully"}},
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list >/dev/null 2>&1",
            ],
            "interval": "10s",
            "timeout": "10s",
            "retries": 20,
        },
        "networks": ["backend"],
        "restart": "unless-stopped",
    }
    services["kafka-topics"] = {
        "image": "apache/kafka:4.0.0@sha256:3f7b939115cd4872e9cee9369d80bd69712fde55f9902f46d793f64848dedc75",
        "entrypoint": ["/bin/bash", "-c"],
        "command": [
            'for topic in requests.observed metrics.observed incidents.opened services.registered audit.events events.failed; do /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic "$$topic" --partitions 3 --replication-factor 1; done'
        ],
        "depends_on": {"kafka": {"condition": "service_healthy"}},
        "networks": ["backend"],
        "restart": "no",
    }
    services["objects"] = {
        "image": "chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882",
        "command": ["mini", "-dir=/data", "-ip=objects", "-ip.bind=0.0.0.0"],
        "environment": {
            "AWS_ACCESS_KEY_ID": "${S3_ACCESS_KEY}",
            "AWS_SECRET_ACCESS_KEY": "${S3_SECRET_KEY}",
            "S3_BUCKET": "helio-objects",
        },
        "volumes": ["object-data:/data"],
        "networks": ["backend"],
        "restart": "unless-stopped",
    }
    services["mailpit"] = {
        "image": "axllent/mailpit:v1.27.4@sha256:df6c2541907e1be6fac21f509927cf6ed771617a1f4b361ef66d97bd05593d2d",
        "ports": ["127.0.0.1:${MAIL_PORT:-8025}:8025"],
        "networks": ["backend"],
        "restart": "unless-stopped",
    }
    for name in NAMES:
        env = {
            "HELIO_SERVICE": name,
            "DATABASE_URL": "postgresql://helio_"
            + name
            + ":${"
            + name.upper()
            + "_DB_PASSWORD}@postgres:5432/helio_"
            + name,
            "INTERNAL_SECRET": "${INTERNAL_SECRET}",
            "KAFKA_BOOTSTRAP": "kafka:9092",
        }
        svc = {
            "image": "helio-platform:v3",
            "build": {"context": "..", "dockerfile": "distributed/Dockerfile"},
            "environment": env,
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "kafka-topics": {"condition": "service_completed_successfully"},
            },
            "networks": ["backend"],
            "restart": "unless-stopped",
            "read_only": True,
            "tmpfs": ["/tmp:size=64m"],
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "pids_limit": 128,
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready',timeout=3)",
                ],
                "interval": "15s",
                "timeout": "5s",
                "start_period": "45s",
                "retries": 5,
            },
        }
        if name == "gateway":
            svc["ports"] = ["127.0.0.1:${HELIO_PORT:-8080}:8000"]
        if name == "auth":
            env.update(AUTH_ENCRYPTION_KEY="${AUTH_ENCRYPTION_KEY}", SETUP_TOKEN="${SETUP_TOKEN}")
        if name == "capacity":
            env.update(
                RUNTIME="docker",
                WORKSPACE_ID="${COMPOSE_PROJECT_NAME}-workloads",
                WORKLOAD_NETWORK="${COMPOSE_PROJECT_NAME}-workloads",
                WORKLOAD_IMAGE="helio-worker:v3",
            )
            svc.update(
                user="0:0",
                volumes=["/var/run/docker.sock:/var/run/docker.sock"],
                networks=["backend", "workloads"],
            )
        if name == "storage":
            env.update(
                S3_ENDPOINT="${S3_ENDPOINT:-http://objects:8333}",
                S3_ACCESS_KEY="${S3_ACCESS_KEY}",
                S3_SECRET_KEY="${S3_SECRET_KEY}",
                S3_BUCKET="${S3_BUCKET:-helio-objects}",
                STORAGE_ENCRYPTION_KEY="${STORAGE_ENCRYPTION_KEY}",
            )
            svc["depends_on"]["objects"] = {"condition": "service_started"}
        if name == "notifications":
            env.update(
                SMTP_HOST="mailpit",
                SMTP_PORT="1025",
                SMTP_FROM="helio@localhost",
                SMTP_TO="operator@localhost",
                SMTP_USER="",
                SMTP_PASSWORD="",
                SLACK_WEBHOOK="",
                NOTIFICATION_WEBHOOK="",
            )
        services[name] = svc
    spec = {
        "name": "${COMPOSE_PROJECT_NAME}",
        "services": services,
        "networks": {"backend": {}, "workloads": {"name": "${COMPOSE_PROJECT_NAME}-workloads"}},
        "volumes": {
            "postgres-data": {},
            "kafka-data": {},
            "object-data": {},
        },
    }
    (ROOT / "compose.yaml").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--show-setup-token", action="store_true")
    display.add_argument(
        "--token-only",
        action="store_true",
        help="Print only the existing token, suitable for piping to Set-Clipboard",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project", default="helio-v3")
    parser.add_argument("--port", default="8080")
    parser.add_argument("--mail-port", default="8025")
    args = parser.parse_args(argv)
    if args.show_setup_token or args.token_only:
        path = args.env_file if args.env_file else ROOT / ".env"
        if not path.is_file():
            raise SystemExit(
                "No configuration exists in this folder. Open the correct project folder and run python run_local.py first."
            )
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        token = values.get("SETUP_TOKEN", "").strip()
        if not token:
            raise SystemExit("SETUP_TOKEN is missing from this project's configuration.")
        print(token if args.token_only else "Server setup token: " + token)
        return
    path = configure(args.env_file, args.project, args.port, args.mail_port)
    compose()
    print("Configuration prepared. Existing secrets were preserved.")
    if args.env_file:
        print("Use the generated environment with docker compose --env-file <path>.")
        print("See README.md for isolated verification deployment commands.")
    else:
        print("From the project root, start with: python run_local.py --open")


if __name__ == "__main__":
    main()
