"""Scoped Docker, Kubernetes, and Amazon ECS workload management."""

import json
import os
import ssl
import time
from urllib.parse import quote
import httpx
import boto3


class DockerRuntime:
    def __init__(self):
        self.scope = os.environ["WORKSPACE_ID"]
        self.network = os.environ["WORKLOAD_NETWORK"]
        self.image = os.environ.get("WORKLOAD_IMAGE", "helio-worker:v3")
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds="/var/run/docker.sock"),
            base_url="http://docker",
            timeout=30,
        )

    def call(self, method, path, body=None):
        r = self.client.request(method, path, json=body)
        r.raise_for_status()
        return (
            r.json()
            if r.content and r.headers.get("content-type", "").startswith("application/json")
            else None
        )

    def inventory(self):
        filters = quote(json.dumps({"label": ["io.helio.workspace=" + self.scope]}))
        containers = self.call("GET", "/containers/json?all=true&filters=" + filters)
        workers = []
        with httpx.Client(timeout=1, trust_env=False) as health:
            for row in containers:
                ip = row["NetworkSettings"]["Networks"].get(self.network, {}).get("IPAddress", "")
                endpoint = "http://" + ip + ":9090" if ip else None
                ready = False
                if row["State"] == "running" and ip:
                    try:
                        ready = health.get("http://" + ip + ":9091/health").status_code == 200
                    except httpx.HTTPError:
                        pass
                workers.append(
                    {"id": row["Id"], "endpoint": endpoint, "ready": ready, "state": row["State"]}
                )
        return workers

    def spawn(self):
        row = self.call(
            "POST",
            "/containers/create",
            {
                "Image": self.image,
                "Cmd": ["python", "worker.py", "--health-port", "9091"],
                "Labels": {"io.helio.workspace": self.scope, "io.helio.role": "managed-workload"},
                "HostConfig": {
                    "NetworkMode": self.network,
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "Memory": 134217728,
                    "NanoCpus": 500000000,
                    "PidsLimit": 64,
                    "RestartPolicy": {"Name": "no"},
                },
            },
        )
        self.call("POST", "/containers/" + row["Id"] + "/start")
        return row["Id"]

    def remove(self, key):
        info = self.call("GET", "/containers/" + key + "/json")
        if info["Config"].get("Labels", {}).get("io.helio.workspace") != self.scope:
            raise RuntimeError("Refusing to manage an unrelated container")
        self.call("DELETE", "/containers/" + key + "?force=true")

    def resize(self, count):
        workers = self.inventory()
        while len(workers) > count:
            self.remove(workers.pop()["id"])
        for _ in range(count - len(workers)):
            self.spawn()
        deadline = time.time() + 12
        while time.time() < deadline:
            workers = self.inventory()
            if len(workers) == count and all(w["ready"] for w in workers):
                return workers
            time.sleep(0.2)
        raise RuntimeError("Requested workers did not pass application readiness checks")


class KubernetesRuntime:
    def __init__(self):
        self.namespace = os.environ.get("KUBE_NAMESPACE", "helio")
        self.deployment = os.environ.get("KUBE_DEPLOYMENT", "checkout-api")
        self.url = os.environ["KUBE_API_URL"].rstrip("/")
        self.workload_url = os.environ.get("KUBE_WORKLOAD_URL", "").rstrip("/")
        self.token_file = os.environ.get("KUBE_TOKEN_FILE", "/run/secrets/kube-token")
        ca = os.environ.get("KUBE_CA_FILE")
        verify = ssl.create_default_context(cafile=ca) if ca else True
        self.client = httpx.Client(timeout=10, verify=verify, trust_env=False)

    def call(self, method, suffix="", body=None):
        with open(self.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        response = self.client.request(
            method,
            self.url
            + f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}"
            + suffix,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/merge-patch+json",
            },
            json=body,
        )
        response.raise_for_status()
        return response.json()

    def state(self):
        data = self.call("GET")
        status = data.get("status", {})
        desired = data["spec"].get("replicas", 1)
        generation = data["metadata"]["generation"]
        converged = (
            status.get("observedGeneration", 0) >= generation
            and status.get("updatedReplicas", 0) == desired
            and status.get("readyReplicas", 0) == desired
            and status.get("availableReplicas", 0) == desired
        )
        return {
            "desired": desired,
            "observed": status.get("readyReplicas", 0),
            "generation": generation,
            "observed_generation": status.get("observedGeneration", 0),
            "converged": converged,
            "resource_version": data["metadata"]["resourceVersion"],
        }

    def scale(self, count):
        before = self.state()
        result = self.call(
            "PATCH",
            "/scale",
            {
                "metadata": {"resourceVersion": before["resource_version"]},
                "spec": {"replicas": count},
            },
        )
        return result

    def health(self):
        if not self.workload_url:
            return False
        try:
            return self.client.get(self.workload_url + "/health").status_code == 200
        except httpx.HTTPError:
            return False


class ECSRuntime:
    """Manage a single ECS workload service using the task role attached to this service."""

    def __init__(self):
        self.cluster = os.environ["ECS_CLUSTER"]
        self.service = os.environ["ECS_WORKLOAD_SERVICE"]
        self.workload_url = os.environ["ECS_WORKLOAD_URL"].rstrip("/")
        self.timeout = int(os.environ.get("ECS_SCALE_TIMEOUT_SECONDS", "180"))
        self.client = boto3.client("ecs", region_name=os.environ.get("AWS_REGION"))

    def _service(self):
        response = self.client.describe_services(cluster=self.cluster, services=[self.service])
        failures = response.get("failures", [])
        services = response.get("services", [])
        if failures or len(services) != 1:
            raise RuntimeError("The configured ECS workload service is unavailable")
        return services[0]

    def state(self):
        service = self._service()
        deployment = next(
            (row for row in service.get("deployments", []) if row.get("status") == "PRIMARY"),
            {},
        )
        desired = service.get("desiredCount", 0)
        running = service.get("runningCount", 0)
        pending = service.get("pendingCount", 0)
        return {
            "desired": desired,
            "observed": running,
            "pending": pending,
            "deployment_id": deployment.get("id"),
            "rollout_state": deployment.get("rolloutState"),
            "converged": running == desired
            and pending == 0
            and deployment.get("rolloutState") == "COMPLETED",
        }

    def scale(self, count):
        self.client.update_service(cluster=self.cluster, service=self.service, desiredCount=count)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            state = self.state()
            if state["desired"] == count and state["converged"]:
                return state
            time.sleep(2)
        raise RuntimeError("ECS did not reach the requested ready task count before timeout")

    def health(self):
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                return client.get(self.workload_url + "/health").status_code == 200
        except httpx.HTTPError:
            return False
