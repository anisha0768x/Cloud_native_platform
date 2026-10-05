"""Targeted logic regressions. Real services are exercised by verify_stack.py."""

from contextlib import contextmanager
from copy import deepcopy
import base64
import json
import secrets
import time
from types import SimpleNamespace
import pytest
import pyotp
from fastapi.testclient import TestClient

from helio.services import auth, capacity
from helio.services.forecast import predict
from helio.runtime import DockerRuntime, KubernetesRuntime
from helio.contracts import Autoscale


class MemoryDB:
    def __init__(self):
        self.data = {}
        self.events = []

    @contextmanager
    def tx(self):
        yield self

    def get(self, c, k, default=None):
        return deepcopy(self.data.get((c, k), default))

    def put(self, c, k, v):
        self.data[c, k] = deepcopy(v)
        return v

    def delete(self, c, k):
        return self.data.pop((c, k), None)

    def rows(self, c, limit=1000, match=None, since=0):
        return [
            deepcopy(v)
            for (collection, key), v in self.data.items()
            if collection == c and all(v.get(k) == value for k, value in (match or {}).items())
        ][:limit]

    def emit(self, topic, data):
        self.events.append((topic, deepcopy(data)))

    def audit(self, *args):
        self.events.append(("audit", args))


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "isolated-service-test-secret")
    monkeypatch.setenv("SETUP_TOKEN", "isolated-first-admin-token")
    monkeypatch.setenv(
        "AUTH_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    db = MemoryDB()
    auth.app.state.ctx = SimpleNamespace(db=db, paused_until=0)
    client = TestClient(auth.app)
    client.headers.update(
        {"X-Helio-Internal": "isolated-service-test-secret", "X-Helio-Loopback": "true"}
    )
    account = {
        "name": "Regression Administrator",
        "email": "regression@example.test",
        "password": "Unique-regression-password-44",
    }
    assert client.post("/api/auth/setup", json=account).status_code == 201
    yield client, db, account
    client.close()


def principal(client, token):
    response = client.post("/internal/introspect", json={"token": token, "bearer": True})
    assert response.status_code == 200, response.text
    return {
        "X-Helio-Actor": base64.urlsafe_b64encode(json.dumps(response.json()).encode()).decode()
    }


def test_mfa_enrollment_revokes_older_sessions_and_verifies_only_current(identity):
    client, db, account = identity
    first = client.post("/api/auth/login", json=account).json()["access_token"]
    older = client.post("/api/auth/login", json=account).json()["access_token"]
    headers = principal(client, first)
    secret = client.post("/api/auth/mfa/enroll", headers=headers).json()["secret"]
    code = pyotp.TOTP(secret).now()
    assert (
        client.post("/api/auth/mfa/confirm", json={"code": code}, headers=headers).status_code
        == 200
    )
    assert (
        client.put(
            "/api/settings/security",
            json={"require_privileged_mfa": True},
            headers=principal(client, first),
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/introspect", json={"token": older, "permission": "admin", "bearer": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/internal/introspect", json={"token": first, "permission": "admin", "bearer": True}
        ).status_code
        == 200
    )
    assert client.post("/api/auth/login", json=account).status_code == 401
    assert client.post("/api/auth/login", json=account | {"code": code}).status_code == 401


def test_mfa_policy_rejects_an_unverified_privileged_session(identity):
    client, db, account = identity
    token = client.post("/api/auth/login", json=account).json()["access_token"]
    db.put("policy", "security", {"require_privileged_mfa": True})
    assert (
        client.post(
            "/internal/introspect", json={"token": token, "permission": "admin", "bearer": True}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/introspect", json={"token": token, "permission": "read", "bearer": True}
        ).status_code
        == 200
    )


def test_csrf_and_idle_expiry(identity):
    client, db, account = identity
    login = client.post("/api/auth/login", json=account).json()
    token = login["access_token"]
    assert (
        client.post(
            "/internal/introspect", json={"token": token, "permission": "admin", "method": "POST"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/introspect",
            json={"token": token, "permission": "admin", "method": "POST", "csrf": login["csrf"]},
        ).status_code
        == 200
    )
    row = db.get("sessions", auth.digest(token))
    row["last_seen"] = time.time() - 1801
    db.put("sessions", row["id"], row)
    assert (
        client.post("/internal/introspect", json={"token": token, "bearer": True}).status_code
        == 401
    )


def test_setup_requires_token_outside_loopback(identity):
    client, db, account = identity
    db.data.clear()
    client.headers["X-Helio-Loopback"] = "false"
    assert client.post("/api/auth/setup", json=account).status_code == 403
    assert (
        client.post(
            "/api/auth/setup", json=account | {"setup_token": "isolated-first-admin-token"}
        ).status_code
        == 201
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "Server setup token: ",
        "SETUP_TOKEN=",
        "First-administrator setup token (keep private): ",
    ],
)
def test_setup_accepts_current_token_copied_from_terminal(identity, prefix):
    client, db, account = identity
    db.data.clear()
    client.headers["X-Helio-Loopback"] = "false"
    result = client.post(
        "/api/auth/setup",
        json=account | {"setup_token": "  " + prefix + "isolated-first-admin-token\r\n"},
    )
    assert result.status_code == 201
    assert len(db.rows("users")) == 1


@pytest.mark.parametrize(
    "token",
    [
        "",
        "old-incorrect-token",
        "Server setup token: old-incorrect-token",
        "incorrect-\u00e9-token",
    ],
)
def test_setup_rejects_wrong_token_without_creating_account(identity, token):
    client, db, account = identity
    db.data.clear()
    client.headers["X-Helio-Loopback"] = "false"
    result = client.post("/api/auth/setup", json=account | {"setup_token": token})
    assert result.status_code == 403
    assert not db.rows("users")
    assert "remote" not in result.json()["detail"]


def test_viewer_denied_by_identity_service(identity):
    client, db, account = identity
    token = client.post("/api/auth/login", json=account).json()["access_token"]
    user = db.rows("users")[0]
    user["role"] = "viewer"
    db.put("users", user["id"], user)
    assert (
        client.post(
            "/internal/introspect", json={"token": token, "permission": "operate", "bearer": True}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "generation,updated,available,converged",
    [(8, 2, 2, False), (9, 0, 2, False), (9, 2, 1, False), (9, 2, 2, True)],
)
def test_kubernetes_verification_requires_observed_generation_and_availability(
    generation, updated, available, converged
):
    runtime = KubernetesRuntime.__new__(KubernetesRuntime)
    runtime.call = lambda *args: {
        "spec": {"replicas": 2},
        "metadata": {"generation": 9, "resourceVersion": "20"},
        "status": {
            "observedGeneration": generation,
            "readyReplicas": 2,
            "updatedReplicas": updated,
            "availableReplicas": available,
        },
    }
    assert runtime.state()["converged"] is converged


def test_running_docker_container_is_not_ready_if_health_fails(monkeypatch):
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.scope = "isolated"
    runtime.network = "test-network"
    runtime.call = lambda *args: [
        {
            "Id": "test-worker",
            "State": "running",
            "NetworkSettings": {"Networks": {"test-network": {"IPAddress": "127.0.0.1"}}},
        }
    ]

    class HTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args):
            return SimpleNamespace(status_code=503)

    monkeypatch.setattr("helio.runtime.httpx.Client", HTTP)
    assert runtime.inventory()[0]["ready"] is False


class Driver:
    def __init__(self):
        self.workers = [
            {"id": "initial", "endpoint": "http://worker:9090", "ready": True, "state": "running"}
        ]
        self.removed = []

    def inventory(self):
        return deepcopy(self.workers)

    def resize(self, count):
        self.workers = [
            {
                "id": "worker-" + str(i),
                "endpoint": "http://worker:9090",
                "ready": True,
                "state": "running",
            }
            for i in range(count)
        ]
        return self.inventory()

    def remove(self, key):
        self.removed.append(key)
        self.workers = [w for w in self.workers if w["id"] != key]


def controller(monkeypatch):
    driver = Driver()
    monkeypatch.setattr(capacity, "DockerRuntime", lambda: driver)
    monkeypatch.setenv("RUNTIME", "docker")
    db = MemoryDB()
    ctx = SimpleNamespace(db=db, threads=[])
    return capacity.Controller(ctx), driver


def test_autoscaler_requires_consecutive_samples_and_respects_cooldown(monkeypatch):
    c, driver = controller(monkeypatch)
    c.db.put(
        "policy",
        "autoscale",
        Autoscale(
            enabled=True, target_rps=2, consecutive_samples=2, cooldown_seconds=30, max_replicas=2
        ).model_dump(),
    )
    monkeypatch.setattr(
        capacity,
        "rpc",
        lambda *args: {"latest": {"request_rate": {"value": 10, "occurred": time.time()}}},
    )
    c.reconcile()
    assert c.desired == 1
    c.reconcile()
    assert c.desired == 2
    c.reconcile()
    assert len(c.db.rows("actions")) == 1


def test_controller_replaces_workers_only_after_repeated_health_failure(monkeypatch):
    c, driver = controller(monkeypatch)
    driver.workers[0]["ready"] = False
    c.reconcile()
    c.reconcile()
    assert driver.removed == []
    c.reconcile()
    assert driver.removed == ["initial"] and c.state()["observed"] == 1


def test_forecast_needs_evidence_and_reports_held_out_errors():
    assert predict([], 60)["status"] == "insufficient_data"
    now = time.time()
    points = [{"occurred": now - 2 * (39 - i), "value": float(i)} for i in range(40)]
    result = predict(points, 60)
    assert result["status"] == "ready" and result["model"] == "Linear trend"
    assert result["mae"] < 0.001 and result["evaluated_samples"] == 10
    assert result["mae"] < result["baseline_mae"]
    assert predict([p | {"occurred": p["occurred"] - 60} for p in points], 60)["status"] == "stale"
