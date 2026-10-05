"""End-to-end checks against an explicitly isolated, real Compose deployment.

Run with --env-file distributed/.env.verify. Never point this at user data:
the recovery and fault checks deliberately change the verification workspace.
No passwords or session tokens are written to the report.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

import httpx
import pyotp

ROOT = Path(__file__).resolve().parents[2]
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


def eventually(check, seconds=90, description="condition"):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, AssertionError, KeyError) as exc:
            last = type(exc).__name__
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {description}; last error: {last}")


class Verification:
    def __init__(self, args):
        self.args = args
        self.env_file = args.env_file.resolve()
        self.env = dict(
            line.split("=", 1)
            for line in self.env_file.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
        self.project = self.env["COMPOSE_PROJECT_NAME"]
        if not self.project.startswith("helio-verify-"):
            raise SystemExit("Refusing: use a dedicated helio-verify-* Compose project.")
        self.docker = (
            shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        )
        self.base = "http://127.0.0.1:" + self.env["HELIO_PORT"]
        self.client = httpx.Client(base_url=self.base, timeout=180, trust_env=False)
        self.results = []
        self.unique = secrets.token_hex(4)
        self.account = {
            "name": "Verification Administrator",
            "email": "verify@example.test",
            "password": "Verification-only-" + self.env["SETUP_TOKEN"][:18],
        }

    def compose(self, *args):
        result = subprocess.run(
            [
                self.docker,
                "compose",
                "--env-file",
                str(self.env_file),
                "-f",
                str(ROOT / "compose.yaml"),
                *args,
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise AssertionError(
                "Compose operation failed: " + " ".join(args[:2]) + "\n" + result.stderr[-1200:]
            )
        return result.stdout

    def internal(self, service, path, method="GET", body=None):
        program = (
            "import json,os,httpx; r=httpx.request("
            + repr(method)
            + ","
            + repr("http://" + service + ":8000" + path)
            + ",headers={'X-Helio-Internal':os.environ['INTERNAL_SECRET']},json="
            + repr(body)
            + ",timeout=40);print(json.dumps({'status':r.status_code,'body':r.json()}))"
        )
        return json.loads(self.compose("exec", "-T", "gateway", "python", "-c", program))

    def catalog_received(self, service_id, metric_name, observation_id):
        program = (
            "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); "
            "print(bool(c.execute(\"SELECT 1 FROM documents WHERE collection='latest' "
            "AND id=%s AND body->>'id'=%s\", "
            + repr((service_id + ":" + metric_name, observation_id))
            + ").fetchone())); c.close()"
        )
        return self.compose("exec", "-T", "catalog", "python", "-c", program).strip() == "True"

    def catalog_breach_count(self, service_id, metric_name):
        program = (
            "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); "
            "r=c.execute(\"SELECT body->>'count' FROM documents WHERE collection='breaches' "
            'AND id=%s", '
            + repr((service_id + ":" + metric_name,))
            + ").fetchone(); print(int(r[0]) if r else 0); c.close()"
        )
        return int(self.compose("exec", "-T", "catalog", "python", "-c", program).strip())

    def api(self, method, path, status=200, **kwargs):
        response = self.client.request(method, path, **kwargs)
        assert (
            response.status_code == status
        ), f"{method} {path}: expected {status}, got {response.status_code}: {response.text[:350]}"
        return (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else response
        )

    def login(self):
        response = self.api("POST", "/api/auth/login", json=self.account)
        self.client.headers["Authorization"] = "Bearer " + response["access_token"]
        self.user = response["user"]
        return response

    @contextmanager
    def step(self, name):
        start = time.monotonic()
        try:
            yield
        except Exception as exc:
            self.results.append({"name": name, "status": "failed", "error": str(exc)[:800]})
            self.save()
            raise
        self.results.append(
            {"name": name, "status": "passed", "seconds": round(time.monotonic() - start, 2)}
        )
        self.save()
        print("PASS " + name, flush=True)

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(
            json.dumps(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "project": self.project,
                    "base_url": self.base,
                    "checks": self.results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def run(self):
        with self.step("Published API routes enforce the authentication boundary"):
            schema = self.client.get("/api/openapi.json").json()
            assert len(schema["paths"]) >= 45
            public = {"/api/auth/setup-status", "/api/auth/setup", "/api/auth/login"}
            with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as anonymous:
                for route, operations in schema["paths"].items():
                    if route in public:
                        continue
                    path = route.replace("{service_id}", "sample").replace(
                        "{incident_id}", "sample"
                    )
                    path = path.replace("{object_id}", "sample").replace("{notice_id}", "sample")
                    path = path.replace("{user_id}", "sample").replace("{event_id}", "sample")
                    path = path.replace("{service}", "sample").replace("{key}", "sample")
                    for method in operations:
                        if method.lower() in {"get", "post", "put", "patch", "delete"}:
                            response = anonymous.request(method.upper(), path)
                            assert response.status_code == 401, (
                                method,
                                route,
                                response.status_code,
                            )

        with self.step("First-administrator setup and authenticated gateway"):
            status = eventually(
                lambda: self.client.get("/api/auth/setup-status").json(),
                description="identity service",
            )
            if status["required"]:
                self.api(
                    "POST",
                    "/api/auth/setup",
                    201,
                    json=self.account | {"setup_token": self.env["SETUP_TOKEN"]},
                )
            login = self.login()
            assert self.api("GET", "/api/auth/me")["user"]["role"] == "admin"
            assert (
                "HttpOnly"
                in self.client.cookies.jar._cookies["127.0.0.1"]["/"]["helio_session"]._rest
            )
            self.api("GET", "/internal/snapshot", 403)

        with self.step("Malformed API payloads return validation errors"):
            self.api(
                "POST",
                "/api/auth/password",
                422,
                json={"current_password": 7, "new_password": "Valid-new-password-44"},
            )
            self.api("POST", "/api/logs/analyze", 422, json={"query": 7})
            self.api("POST", "/api/v1/notifications/send", 422, json={"incident_id": 7})
            self.api(
                "POST",
                "/api/maintenance/outcomes",
                422,
                json={"observation_id": "sample", "failed": "false", "evidence": "Evidence note"},
            )

        with self.step("Twelve real services and private PostgreSQL ownership"):
            data = eventually(
                lambda: self.api("GET", "/api/cloud/network"), description="service topology"
            )
            assert {s["service"] for s in data["services"]} == set(NAMES)
            ps = [
                json.loads(line)
                for line in self.compose("ps", "--format", "json").splitlines()
                if line
            ]
            assert all(
                any(c["Service"] == name and c["State"] == "running" for c in ps) for name in NAMES
            )
            program = "import os,psycopg;from urllib.parse import urlsplit,urlunsplit; u=urlsplit(os.environ['DATABASE_URL']); target=urlunsplit((u.scheme,u.netloc,'/helio_auth','',''));\ntry:\n psycopg.connect(target,connect_timeout=5);print('UNEXPECTED_ACCESS')\nexcept psycopg.OperationalError:\n print('DENIED')"
            assert "DENIED" in self.compose("exec", "-T", "metrics", "python", "-c", program)

        with self.step("RBAC, CSRF and last-administrator protection"):
            viewer = self.api(
                "POST",
                "/api/users",
                201,
                json={
                    "name": "Verification Viewer",
                    "email": self.unique + "@viewer.test",
                    "password": "Viewer-test-password-42",
                    "role": "viewer",
                },
            )["user"]
            with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as browser:
                session = browser.post(
                    "/api/auth/login",
                    json={"email": viewer["email"], "password": "Viewer-test-password-42"},
                ).json()
                assert (
                    browser.post(
                        "/api/kubernetes/scale",
                        json={"replicas": 2},
                        headers={"Authorization": "Bearer " + session["access_token"]},
                    ).status_code
                    == 403
                )
                assert browser.get("/api/services").status_code == 200
            with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as browser:
                cookie_session = browser.post("/api/auth/login", json=self.account).json()
                assert (
                    browser.put(
                        "/api/settings/thresholds",
                        json={
                            "latency_ms": 100,
                            "error_percent": 5,
                            "consecutive_windows": 2,
                            "window_seconds": 30,
                        },
                    ).status_code
                    == 403
                )
                assert (
                    browser.post(
                        "/api/auth/logout", headers={"X-CSRF-Token": cookie_session["csrf"]}
                    ).status_code
                    == 200
                )
            self.api(
                "PATCH",
                "/api/users/" + self.user["id"],
                409,
                json={"role": "viewer", "active": True},
            )
            self.api("POST", "/api/kubernetes/scale", 422, json={"replicas": 100})
            self.api(
                "POST",
                "/api/kubernetes/scale",
                404,
                json={"deployment": "unmanaged", "replicas": 1},
            )

        with self.step("Measured workload, actual container scaling and Kafka telemetry"):
            eventually(
                lambda: self.api("GET", "/api/kubernetes")["runtime"]["observed"] >= 1,
                description="healthy worker",
            )
            change = self.api("POST", "/api/kubernetes/scale", json={"replicas": 2})
            assert change["status"] == "verified" and change["observed"] == 2
            run = self.api(
                "POST",
                "/api/workload/load",
                202,
                json={"total": 40, "concurrency": 4, "work_ms": 20},
            )
            result = eventually(
                lambda: next(
                    (
                        r
                        for r in self.api("GET", "/api/workload/runs")["runs"]
                        if r["id"] == run["id"] and r["status"] == "completed"
                    ),
                    None,
                ),
                description="real HTTP workload",
            )
            assert (
                result["completed"] == 40
                and result["errors"] == 0
                and result["p95"] > 0
                and result["rps"] > 0
            )
            eventually(
                lambda: self.api("GET", "/api/services/svc-worker")["service"]["request_count"]
                >= 40,
                description="Kafka metrics ingestion",
            )
            eventually(
                lambda: len(self.api("GET", "/api/logs?service_id=svc-worker&limit=100")["entries"])
                >= 40,
                description="Kafka log ingestion",
            )

        with self.step("Service registration, fresh metric events and incident lifecycle"):
            svc = self.api("POST", "/api/services", 201, json={"name": "probe-" + self.unique})
            self.service_id = svc["id"]
            assert svc["status"] == "unknown"
            first = self.api(
                "POST",
                "/api/v1/metrics/ingest",
                201,
                json={
                    "service_id": self.service_id,
                    "metric_name": "latency_p95_ms",
                    "value": 2000,
                },
            )
            eventually(
                lambda: self.catalog_received(self.service_id, "latency_p95_ms", first["id"]),
                description="first distinct breach delivered to catalog",
            )
            eventually(
                lambda: self.catalog_breach_count(self.service_id, "latency_p95_ms") == 1,
                description="first distinct breach counted once",
            )
            assert not any(
                a["service_id"] == self.service_id
                for a in self.api("GET", "/api/alerts?status=all")["alerts"]
            )
            self.api(
                "POST",
                "/api/v1/metrics/ingest",
                201,
                json={
                    "service_id": self.service_id,
                    "metric_name": "latency_p95_ms",
                    "value": 2000,
                },
            )
            self.incident = eventually(
                lambda: next(
                    (
                        a
                        for a in self.api("GET", "/api/alerts?status=all")["alerts"]
                        if a["service_id"] == self.service_id
                    ),
                    None,
                ),
                description="measured threshold incident",
            )
            key = self.incident["id"]
            self.api("POST", "/api/alerts/" + key + "/acknowledge")
            self.api(
                "PATCH",
                "/api/alerts/" + key,
                json={"status": "investigating", "note": "Investigating submitted evidence"},
            )
            self.api(
                "PATCH",
                "/api/alerts/" + key,
                409,
                json={"status": "resolved", "note": "Premature resolution must fail"},
            )
            self.api(
                "POST",
                "/api/v1/metrics/ingest",
                201,
                json={"service_id": self.service_id, "metric_name": "latency_p95_ms", "value": 10},
            )
            eventually(
                lambda: not self.api("GET", "/api/alerts/" + key)["incident"]["condition_active"],
                description="fresh recovery observation",
            )
            self.api(
                "PATCH",
                "/api/alerts/" + key,
                json={"status": "resolved", "note": "Verified fresh below-threshold metric"},
            )
            assert len(self.api("GET", "/api/alerts/" + key)["events"]) >= 5

        with self.step("Evidence-linked diagnostics and credential redaction"):
            row = self.api(
                "POST",
                "/api/logs/ingest",
                201,
                json={
                    "service_id": self.service_id,
                    "level": "ERROR",
                    "message": "Dependency timeout for person@example.test token=topsecret-value",
                },
            )
            entry = self.api("GET", "/api/logs?service_id=" + self.service_id)["entries"][0]
            assert (
                "topsecret-value" not in entry["message"]
                and "person@example.test" not in entry["message"]
            )
            diagnosis = self.api("POST", "/api/logs/analyze", json={"service_id": self.service_id})
            assert diagnosis["findings"][0]["title"] == "Dependency timeout"
            assert row["id"] in diagnosis["findings"][0]["log_ids"]

        with self.step("Asynchronous local SMTP delivery and duplicate suppression"):
            key = self.incident["id"]
            notice = eventually(
                lambda: next(
                    (
                        n
                        for n in self.api("GET", "/api/v1/notifications")["notifications"]
                        if n["incident_id"] == key and n["channel"] == "inbox"
                    ),
                    None,
                ),
                description="Kafka inbox notification",
            )
            with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as viewer:
                login = viewer.post(
                    "/api/auth/login",
                    json={
                        "email": self.unique + "@viewer.test",
                        "password": "Viewer-test-password-42",
                    },
                ).json()
                assert (
                    viewer.post(
                        "/api/notifications/" + notice["id"] + "/read",
                        headers={"Authorization": "Bearer " + login["access_token"]},
                    ).status_code
                    == 403
                )
            self.api("POST", "/api/notifications/" + notice["id"] + "/read")
            sent = self.api(
                "POST", "/api/v1/notifications/send", json={"incident_id": key, "channel": "email"}
            )
            duplicate = self.api(
                "POST", "/api/v1/notifications/send", json={"incident_id": key, "channel": "email"}
            )
            assert sent["id"] == duplicate["id"]
            eventually(
                lambda: any(
                    n["id"] == sent["id"] and n["status"] == "accepted"
                    for n in self.api("GET", "/api/v1/notifications")["notifications"]
                ),
                description="SMTP acknowledgement",
            )
            mail_url = "http://127.0.0.1:" + self.env["MAIL_PORT"] + "/api/v1/messages"
            assert httpx.get(mail_url, trust_env=False).json()["total"] >= 1

        with self.step("Real S3 bytes, encryption, versions and retention"):
            self.content = b"Verified real object bytes: " + secrets.token_bytes(80)
            name = "verified-" + self.unique + ".bin"
            self.object = self.api(
                "POST",
                "/api/cloud/storage",
                201,
                files={"file": (name, self.content, "application/octet-stream")},
                data={"retention_days": "1"},
            )
            second = self.api(
                "POST",
                "/api/cloud/storage",
                201,
                files={"file": (name, b"Second version", "application/octet-stream")},
                data={"retention_days": "0"},
            )
            assert self.object["version"] == 1 and second["version"] == 2
            response = self.api("GET", "/api/cloud/storage/" + self.object["id"] + "/download")
            assert (
                response.content == self.content
                and response.headers["x-content-sha256"] == hashlib.sha256(self.content).hexdigest()
            )
            self.api("DELETE", "/api/cloud/storage/" + self.object["id"], 409)
            self.api("DELETE", "/api/cloud/storage/" + second["id"])
            program = (
                "from helio.services.storage import Objects;import hashlib; o=Objects();blob=o.s3.get_object(Bucket=o.bucket,Key="
                + repr("objects/" + self.object["id"])
                + ")['Body'].read(); print(hashlib.sha256(blob).hexdigest())"
            )
            assert (
                self.compose("exec", "-T", "storage", "python", "-c", program).strip()
                != self.object["checksum"]
            )

        with self.step("Cost arithmetic and explicit forecast/maintenance limits"):
            costs = self.api("GET", "/api/cloud/costs")
            assert abs(costs["total"] - sum(r["cost"] for r in costs["line_items"])) < 0.000001
            assert "not provider billing" in costs["mode"]
            forecast = eventually(
                lambda: self.api("GET", "/api/predictions"), description="forecast response"
            )
            assert forecast["status"] in {"ready", "insufficient_data", "stale"}
            if forecast["status"] == "ready":
                assert forecast["evaluated_samples"] > 0 and forecast["mae"] >= 0
            maintenance = self.api("GET", "/api/maintenance")
            assert (
                maintenance["model_status"] == "not_trained"
                and maintenance["failure_probability"] is None
            )

        if self.args.faults:
            self.faults()

        with self.step("Encrypted backup, PostgreSQL restore verification and real recovery"):
            backup = self.api("POST", "/api/backups", 201)
            verified = self.api("POST", "/api/backups/" + backup["id"] + "/verify")
            assert verified["status"] == "verified" and all(
                name in verified["restored_rows"] for name in NAMES
            )
            changed = self.api(
                "POST", "/api/services", 201, json={"name": "after-backup-" + self.unique}
            )
            self.api(
                "POST",
                "/api/backups/" + backup["id"] + "/restore",
                403,
                json={"confirmation": "wrong", "password": self.account["password"]},
            )
            restored = self.api(
                "POST",
                "/api/backups/" + backup["id"] + "/restore",
                json={"confirmation": backup["id"], "password": self.account["password"]},
            )
            assert restored["status"] == "restored" and len(restored["services"]) == 12
            self.api("GET", "/api/auth/me", 401)
            self.login()
            self.api("GET", "/api/services/" + changed["id"], 404)
            assert (
                self.api("GET", "/api/cloud/storage/" + self.object["id"] + "/download").content
                == self.content
            )

        with self.step("MFA enrollment revokes old sessions and blocks unverified privilege"):
            user = self.api(
                "POST",
                "/api/users",
                201,
                json={
                    "name": "MFA Verification",
                    "email": self.unique + "@mfa.test",
                    "password": "MFA-test-password-42",
                    "role": "admin",
                },
            )["user"]
            credentials = {"email": user["email"], "password": "MFA-test-password-42"}
            with httpx.Client(base_url=self.base, trust_env=False, timeout=30) as mfa:
                first = mfa.post("/api/auth/login", json=credentials).json()
                older = mfa.post("/api/auth/login", json=credentials).json()
                mfa.headers["Authorization"] = "Bearer " + first["access_token"]
                secret = mfa.post("/api/auth/mfa/enroll").json()["secret"]
                code = pyotp.TOTP(secret).now()
                assert mfa.post("/api/auth/mfa/confirm", json={"code": code}).status_code == 200
                assert (
                    mfa.put(
                        "/api/settings/security", json={"require_privileged_mfa": True}
                    ).status_code
                    == 200
                )
                assert (
                    mfa.get(
                        "/api/auth/me", headers={"Authorization": "Bearer " + older["access_token"]}
                    ).status_code
                    == 401
                )
                self.api("POST", "/api/services", 403, json={"name": "blocked-" + self.unique})
                assert (
                    mfa.post("/api/auth/login", json=credentials | {"code": code}).status_code
                    == 401
                )
                assert (
                    mfa.put(
                        "/api/settings/security", json={"require_privileged_mfa": False}
                    ).status_code
                    == 200
                )

        with self.step("Final service readiness, audit persistence and API specification"):
            eventually(
                lambda: all(
                    s["status"] == "ready"
                    for s in self.api("GET", "/api/cloud/network")["services"]
                ),
                description="all twelve services ready",
            )
            events = self.api("GET", "/api/v1/audit")["events"]
            assert any(e["action"] == "backup.restored" for e in events)
            assert len(self.api("GET", "/api/openapi.json")["paths"]) >= 30
        print(
            f"{len(self.results)} real-stack checks passed. Report: {self.args.report}", flush=True
        )

    def faults(self):
        with self.step("Automatic scaling observes demand, bounds and cooldown"):
            self.api("POST", "/api/kubernetes/scale", json={"replicas": 1})
            self.api(
                "PUT",
                "/api/kubernetes/autoscale",
                json={
                    "enabled": True,
                    "min_replicas": 1,
                    "max_replicas": 2,
                    "target_rps": 0.1,
                    "cooldown_seconds": 5,
                    "consecutive_samples": 2,
                    "self_heal": True,
                },
            )
            run = self.api(
                "POST",
                "/api/workload/load",
                202,
                json={"total": 80, "concurrency": 4, "work_ms": 20},
            )
            eventually(
                lambda: any(
                    r["id"] == run["id"] and r["status"] == "completed"
                    for r in self.api("GET", "/api/workload/runs")["runs"]
                ),
                description="demand workload",
            )
            eventually(
                lambda: any(
                    a["actor"] == "autoscaler" and a["desired"] == 2 and a["status"] == "verified"
                    for a in self.api("GET", "/api/kubernetes")["actions"]
                ),
                description="automatic scale-up",
            )
            assert self.api("GET", "/api/kubernetes")["runtime"]["desired"] <= 2
            self.api(
                "PUT",
                "/api/kubernetes/autoscale",
                json={
                    "enabled": False,
                    "min_replicas": 1,
                    "max_replicas": 2,
                    "target_rps": 5,
                    "cooldown_seconds": 30,
                    "consecutive_samples": 2,
                    "self_heal": True,
                },
            )

        with self.step("Worker failure is detected and a new healthy container replaces it"):
            before = self.api("GET", "/api/kubernetes")["runtime"]
            key = before["workers"][0]["id"]
            # The label check prevents the fault test from stopping unrelated containers.
            inspected = subprocess.run(
                [self.docker, "inspect", key], capture_output=True, text=True, check=True
            )
            labels = json.loads(inspected.stdout)[0]["Config"]["Labels"]
            assert self.project in json.dumps(labels)
            subprocess.run([self.docker, "stop", key], capture_output=True, check=True, timeout=30)
            eventually(
                lambda: (r := self.api("GET", "/api/kubernetes")["runtime"])["observed"]
                == before["desired"]
                and key not in [w["id"] for w in r["workers"]],
                description="self-healing replacement",
            )

        with self.step(
            "Kafka outage retains events and consumer restart avoids duplicate incidents"
        ):
            self.compose("stop", "kafka")
            try:
                svc = self.api("POST", "/api/services", 201, json={"name": "outage-" + self.unique})
                observation = self.api(
                    "POST",
                    "/api/v1/metrics/ingest",
                    201,
                    json={"service_id": svc["id"], "metric_name": "error_percent", "value": 40},
                )
                pending = self.internal("metrics", "/ready")
                assert pending["body"]["outbox_pending"] >= 1
            finally:
                self.compose("start", "kafka")

            # A delayed observation can be stale by the time Kafka recovers.
            # Verify delivery from the durable outbox independently of alert freshness.
            eventually(
                lambda: self.catalog_received(svc["id"], "error_percent", observation["id"]),
                seconds=120,
                description="outbox replay after Kafka recovery",
            )
            # Alerting deliberately requires fresh evidence, so submit a new
            # observation after recovery rather than alerting on a stale event.
            fresh = self.api(
                "POST",
                "/api/v1/metrics/ingest",
                201,
                json={"service_id": svc["id"], "metric_name": "error_percent", "value": 40},
            )
            eventually(
                lambda: self.catalog_received(svc["id"], "error_percent", fresh["id"]),
                description="fresh post-recovery observation",
            )
            eventually(
                lambda: self.catalog_breach_count(svc["id"], "error_percent") >= 1,
                description="fresh post-recovery breach evaluation",
            )
            self.api(
                "POST",
                "/api/v1/metrics/ingest",
                201,
                json={"service_id": svc["id"], "metric_name": "error_percent", "value": 40},
            )
            eventually(
                lambda: any(
                    a["service_id"] == svc["id"]
                    for a in self.api("GET", "/api/alerts?status=all")["alerts"]
                ),
                seconds=120,
                description="fresh incident evidence after Kafka recovery",
            )
            self.compose("restart", "catalog", "notifications")
            eventually(
                lambda: self.client.get("/api/services").status_code == 200,
                description="consumer restart",
            )
            assert (
                len(
                    [
                        a
                        for a in self.api("GET", "/api/alerts?status=all")["alerts"]
                        if a["service_id"] == svc["id"]
                    ]
                )
                == 1
            )

        with self.step("Unavailable dependency returns 503 and recovers without fabricated data"):
            self.compose("stop", "forecast")
            try:
                self.api("GET", "/api/predictions", 503)
                assert self.api("GET", "/api/cloud/storage")["objects"]
            finally:
                self.compose("start", "forecast")
            eventually(
                lambda: self.client.get("/api/predictions").status_code == 200,
                description="forecast service restart",
            )

        with self.step("Failed-event quarantine and successful replay after repairing the cause"):
            trace = "verification-failure-" + self.unique
            # Real PostgreSQL constraint: deliberately reject this one isolated fixture.
            program = (
                "from helio.common import Database;from psycopg import sql; d=Database();d.sql(sql.SQL(\"ALTER TABLE documents ADD CONSTRAINT verification_reject CHECK (collection!='logs' OR body->>'trace_id' IS DISTINCT FROM {})\").format(sql.Literal("
                + repr(trace)
                + ")))"
            )
            self.compose("exec", "-T", "logs", "python", "-c", program)
            try:
                fixture = {
                    "service_id": self.service_id,
                    "occurred": time.time(),
                    "latency": 1.0,
                    "status": 200,
                    "trace_id": trace,
                    "bytes": 0,
                }
                program = (
                    "from helio.common import Database; print(Database().emit('requests.observed',"
                    + repr(fixture)
                    + "))"
                )
                event_id = self.compose("exec", "-T", "capacity", "python", "-c", program).strip()
                eventually(
                    lambda: any(
                        e["id"] == event_id and e["service"] == "logs"
                        for e in self.api("GET", "/api/events/failures")["events"]
                    ),
                    description="failed-event quarantine",
                )
                self.api("POST", "/api/events/logs/" + event_id + "/retry", 409)
            finally:
                self.compose(
                    "exec",
                    "-T",
                    "logs",
                    "python",
                    "-c",
                    "from helio.common import Database;Database().sql('ALTER TABLE documents DROP CONSTRAINT verification_reject')",
                )
            assert (
                self.api("POST", "/api/events/logs/" + event_id + "/retry")["status"] == "processed"
            )
            assert not any(
                e["id"] == event_id for e in self.api("GET", "/api/events/failures")["events"]
            )
            assert (
                len(
                    [
                        e
                        for e in self.api("GET", "/api/logs?service_id=" + self.service_id)[
                            "entries"
                        ]
                        if e.get("trace_id") == trace
                    ]
                )
                == 1
            )

        with self.step("Notification retries remain independent of metric collection"):
            incident = next(
                a
                for a in self.api("GET", "/api/alerts?status=all")["alerts"]
                if a["service_id"] == svc["id"]
            )
            self.compose("stop", "mailpit")
            try:
                notice = self.api(
                    "POST",
                    "/api/v1/notifications/send",
                    json={"incident_id": incident["id"], "channel": "email"},
                )
                eventually(
                    lambda: any(
                        n["id"] == notice["id"] and n["status"] == "failed" and n["attempts"] == 3
                        for n in self.api("GET", "/api/v1/notifications")["notifications"]
                    ),
                    seconds=60,
                    description="bounded delivery retries",
                )
                collection = self.internal("metrics", "/internal/status")["body"]
                assert collection["error"] is None
                assert (
                    time.time() - datetime.fromisoformat(collection["last_success"]).timestamp()
                    < 15
                )
            finally:
                self.compose("start", "mailpit")
            self.api("POST", "/api/notifications/" + notice["id"] + "/retry")
            eventually(
                lambda: any(
                    n["id"] == notice["id"] and n["status"] == "accepted"
                    for n in self.api("GET", "/api/v1/notifications")["notifications"]
                ),
                description="delivery after receiver recovery",
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument(
        "--report", type=Path, default=Path("test-results/distributed-verification.json")
    )
    Verification(parser.parse_args()).run()
