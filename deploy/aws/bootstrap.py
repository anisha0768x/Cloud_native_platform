"""One-shot AWS bootstrap: create isolated PostgreSQL roles/databases and Kafka topics."""

import json
import os
import time

import psycopg
from psycopg import sql
from confluent_kafka.admin import AdminClient, NewTopic

from helio.common import NAMES, kafka_config


def bootstrap_databases():
    passwords = json.loads(os.environ["SERVICE_PASSWORDS"])
    missing = set(NAMES) - set(passwords)
    if missing:
        raise RuntimeError("Missing database passwords for: " + ", ".join(sorted(missing)))
    with psycopg.connect(os.environ["MASTER_DATABASE_URL"], autocommit=True) as connection:
        for name in NAMES:
            role = "helio_" + name
            password = passwords[name]
            connection.execute(
                sql.SQL(
                    "DO $$ BEGIN IF NOT EXISTS "
                    "(SELECT FROM pg_roles WHERE rolname = {}) THEN "
                    "CREATE ROLE {} LOGIN; END IF; END $$"
                ).format(
                    sql.Literal(role), sql.Identifier(role)
                )
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            # RDS master is not a superuser; OWNER requires membership in the role.
            connection.execute(
                sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role))
            )
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (role,)
            ).fetchone()
            if not exists:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(role), sql.Identifier(role)
                    )
                )


def bootstrap_topics():
    topics = (
        "requests.observed",
        "metrics.observed",
        "incidents.opened",
        "services.registered",
        "audit.events",
        "events.failed",
    )
    replication = int(os.environ.get("KAFKA_REPLICATION_FACTOR", "3"))
    last_error = None
    for attempt in range(12):
        try:
            admin = AdminClient(kafka_config())
            futures = admin.create_topics(
                [NewTopic(topic, num_partitions=3, replication_factor=replication) for topic in topics]
            )
            for topic, future in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    if "TOPIC_ALREADY_EXISTS" not in str(exc):
                        raise RuntimeError("Unable to create Kafka topic " + topic) from exc
            return
        except Exception as exc:
            last_error = exc
            time.sleep(5)
    raise RuntimeError("Unable to reach Kafka at " + os.environ.get("KAFKA_BOOTSTRAP", "")) from last_error


if __name__ == "__main__":
    bootstrap_databases()
    bootstrap_topics()
    print("AWS database roles, databases, and Kafka topics are ready.")
