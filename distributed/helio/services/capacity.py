from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import os
import threading
import time
import httpx
from fastapi import Request
from helio.common import actor, fail, rpc, service_app, stamp, uid
from helio.contracts import Autoscale, Scale, Load
from helio.runtime import DockerRuntime, ECSRuntime, KubernetesRuntime


def percentile(values):
    return round(sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)], 2) if values else None


class Controller:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.lock = threading.RLock()
        self.workers = []
        self.cursor = 0
        self.active_job = None
        self.failed = {}
        self.last_change = 0
        self.high = 0
        self.low = 0
        self.error = None
        self.mode = os.environ.get("RUNTIME", "docker")
        drivers = {"docker": DockerRuntime, "kubernetes": KubernetesRuntime, "ecs": ECSRuntime}
        if self.mode not in drivers:
            raise RuntimeError("RUNTIME must be docker, kubernetes, or ecs")
        self.driver = drivers[self.mode]()
        self.desired = self.db.get("policy", "capacity", {"replicas": 1})["replicas"]
        if not self.db.get("policy", "autoscale"):
            self.db.put("policy", "autoscale", Autoscale().model_dump())
        for run in self.db.rows("runs", 100, {"status": "running"}):
            run.update(
                status="interrupted", finished=time.time(), error="Capacity service restarted"
            )
            self.db.put("runs", run["id"], run)

    def state(self):
        with self.lock:
            if self.mode in {"kubernetes", "ecs"}:
                try:
                    data = self.driver.state()
                    ready = self.driver.health()
                    self.error = None
                    return data | {
                        "backend": self.mode,
                        "deployment": getattr(
                            self.driver, "deployment", getattr(self.driver, "service", None)
                        ),
                        "namespace": getattr(
                            self.driver, "namespace", getattr(self.driver, "cluster", None)
                        ),
                        "connected": True,
                        "workload_connected": ready,
                        "error": None,
                        "workers": [],
                    }
                except Exception as exc:
                    self.error = type(exc).__name__
            return {
                "backend": self.mode,
                "deployment": "checkout-api",
                "namespace": os.environ.get("WORKSPACE_ID", "helio"),
                "desired": self.desired,
                "observed": sum(w["ready"] for w in self.workers),
                "workers": self.workers,
                "connected": not bool(self.error),
                "workload_connected": any(w["ready"] for w in self.workers),
                "error": self.error,
            }

    def change(self, replicas, who, reason="manual"):
        with self.lock:
            if self.active_job and reason == "manual":
                fail(
                    409,
                    "Wait for the active workload test to finish; automatic scale-up remains available if enabled.",
                )
            before = self.state()
            row = {
                "id": uid("scale"),
                "actor": who,
                "backend": self.mode,
                "deployment": "checkout-api",
                "previous": before["observed"],
                "desired": replicas,
                "observed": before["observed"],
                "status": "requested",
                "error": None,
                "reason": reason,
                "created_at": stamp(),
            }
            self.db.put("actions", row["id"], row)
            try:
                if self.mode == "docker":
                    self.workers = self.driver.resize(replicas)
                else:
                    self.driver.scale(replicas)
                self.desired = replicas
                self.db.put("policy", "capacity", {"replicas": replicas})
                self.last_change = time.time()
                self.error = None
                after = self.state()
                verified = after["observed"] == replicas and (
                    self.mode == "docker" or after["converged"] and after["workload_connected"]
                )
                row.update(observed=after["observed"], status="verified" if verified else "pending")
                for old in self.db.rows("actions", 100, {"status": "pending"}):
                    if old["id"] != row["id"]:
                        old["status"] = "superseded"
                        self.db.put("actions", old["id"], old)
            except Exception as exc:
                row.update(status="failed", error=type(exc).__name__)
                self.error = "Runtime action failed; inspect service health and action history."
            with self.db.tx():
                self.db.put("actions", row["id"], row)
                self.db.audit(who, "capacity." + row["status"], row["id"], row)
            if row["status"] == "failed":
                fail(503, self.error)
            return row

    def reconcile(self):
        with self.lock:
            policy = self.db.get("policy", "autoscale")
            now = time.time()
            try:
                if self.mode == "docker":
                    self.workers = self.driver.inventory()
                    for worker in self.workers:
                        self.failed[worker["id"]] = (
                            0 if worker["ready"] else self.failed.get(worker["id"], 0) + 1
                        )
                        if policy["self_heal"] and self.failed[worker["id"]] >= 3:
                            self.driver.remove(worker["id"])
                            self.db.audit(
                                "controller",
                                "worker.replaced",
                                worker["id"],
                                {"reason": "Three failed application health checks"},
                            )
                    self.workers = self.driver.inventory()
                    if policy["self_heal"] and len(self.workers) < self.desired:
                        self.workers = self.driver.resize(self.desired)
                    self.error = None
                else:
                    state = self.state()
                    if state.get("converged") and state["workload_connected"]:
                        for action in self.db.rows("actions", 100, {"status": "pending"}):
                            if action["desired"] == state["desired"]:
                                action.update(status="verified", observed=state["observed"])
                                self.db.put("actions", action["id"], action)
            except Exception as exc:
                self.error = "Runtime health check failed: " + type(exc).__name__
                return
        state = self.state()
        self.db.emit(
            "metrics.observed",
            {
                "id": uid("metric"),
                "service_id": "svc-worker",
                "name": "ready_replicas",
                "value": state["observed"],
                "occurred": now,
                "source": self.mode + "-application-health",
            },
        )
        if not policy["enabled"] or now - self.last_change < policy["cooldown_seconds"]:
            return
        summary = rpc("metrics", "/internal/summary/svc-worker")
        point = summary["latest"].get("request_rate")
        if not point or now - point["occurred"] > 30:
            return
        target = max(
            policy["min_replicas"],
            min(policy["max_replicas"], math.ceil(point["value"] / policy["target_rps"])),
        )
        self.high = self.high + 1 if target > self.desired else 0
        self.low = self.low + 1 if target < self.desired else 0
        if self.high >= policy["consecutive_samples"]:
            self.change(min(self.desired + 1, target), "autoscaler", "measured demand")
            self.high = 0
        elif self.low >= policy["consecutive_samples"] and not self.active_job:
            self.change(max(self.desired - 1, target), "autoscaler", "sustained low demand")
            self.low = 0

    def endpoint(self):
        with self.lock:
            if self.mode in {"kubernetes", "ecs"}:
                if not self.driver.workload_url:
                    fail(503, "Configure the managed workload service URL.")
                return self.driver.workload_url
            ready = [w for w in self.workers if w["ready"]]
            if not ready:
                fail(503, "No healthy workload replicas.")
            worker = ready[self.cursor % len(ready)]
            self.cursor += 1
            return worker["endpoint"]

    def load(self, payload, who):
        with self.lock:
            if self.active_job:
                fail(409, "A workload test is already running.")
            self.endpoint()
            key = uid("load")
            self.active_job = key
            row = {
                "id": key,
                "actor": who,
                "status": "running",
                "total": payload.total,
                "completed": 0,
                "errors": 0,
                "concurrency": payload.concurrency,
                "work_ms": payload.work_ms,
                "started": time.time(),
                "finished": None,
                "p95": None,
                "rps": None,
                "error": None,
            }
            self.db.put("runs", key, row)
            self.db.audit(who, "workload.started", key)

        def run():
            durations = []
            began = time.perf_counter()
            try:
                with httpx.Client(
                    trust_env=False,
                    timeout=20,
                    limits=httpx.Limits(max_connections=payload.concurrency),
                ) as client:

                    def request_one(_):
                        start = time.perf_counter()
                        status = 503
                        size = 0
                        try:
                            r = client.post(
                                self.endpoint() + "/work", json={"work_ms": payload.work_ms}
                            )
                            status = r.status_code
                            size = len(r.content)
                        except Exception:
                            pass
                        elapsed = (time.perf_counter() - start) * 1000
                        self.db.emit(
                            "requests.observed",
                            {
                                "service_id": "svc-worker",
                                "occurred": time.time(),
                                "latency": elapsed,
                                "status": status,
                                "bytes": size,
                                "trace_id": uid("trace"),
                            },
                        )
                        return elapsed, status

                    with ThreadPoolExecutor(max_workers=payload.concurrency) as pool:
                        for result in as_completed(
                            [pool.submit(request_one, i) for i in range(payload.total)]
                        ):
                            latency, status = result.result()
                            durations.append(latency)
                            row["completed"] += 1
                            row["errors"] += int(status >= 400)
                            self.db.put("runs", key, row)
                row.update(
                    status="completed",
                    p95=percentile(durations),
                    rps=round(len(durations) / (time.perf_counter() - began), 3),
                    finished=time.time(),
                )
            except Exception as exc:
                row.update(status="failed", error=type(exc).__name__, finished=time.time())
            finally:
                with self.lock:
                    self.active_job = None
                    self.db.put("runs", key, row)

        thread = threading.Thread(target=run, daemon=True)
        self.ctx.threads.append(thread)
        thread.start()
        return {"id": key, "status": "running"}


def install(app):
    def initialize(ctx):
        ctx.controller = Controller(ctx)

    app.state.initialize = initialize

    def after_restore(ctx):
        c = ctx.controller
        with c.lock:
            c.desired = ctx.db.get("policy", "capacity", {"replicas": 1})["replicas"]
            c.high = c.low = 0
            if c.mode == "docker":
                c.workers = c.driver.resize(c.desired)
            else:
                c.driver.scale(c.desired)

    app.state.after_restore = after_restore

    def controller():
        return app.state.ctx.controller

    @app.get("/internal/state")
    def state():
        return controller().state()

    @app.get("/api/kubernetes")
    def capacity(request: Request):
        actor(request)
        c = controller()
        return {
            "runtime": c.state(),
            "actions": c.db.rows("actions", 50),
            "limits": {"min": 1, "max": 8},
            "autoscale": c.db.get("policy", "autoscale"),
        }

    @app.post("/api/kubernetes/scale")
    def scale(payload: Scale, request: Request):
        user = actor(request, "operate")
        if payload.deployment != "checkout-api":
            fail(404, "Unknown managed deployment.")
        return controller().change(payload.replicas, user["email"])

    @app.put("/api/kubernetes/autoscale")
    def policy(payload: Autoscale, request: Request):
        user = actor(request, "admin")
        if payload.min_replicas > payload.max_replicas:
            fail(422, "Minimum replicas cannot exceed maximum replicas.")
        c = controller()
        with c.lock:
            c.db.put("policy", "autoscale", payload.model_dump())
            c.high = c.low = 0
            c.db.audit(user["email"], "autoscale.updated", "checkout-api", payload.model_dump())
        return payload

    @app.post("/api/workload/load", status_code=202)
    def load(payload: Load, request: Request):
        return controller().load(payload, actor(request, "operate")["email"])

    @app.get("/api/workload/runs")
    def runs(request: Request):
        actor(request)
        c = controller()
        with c.lock:
            return {"runs": c.db.rows("runs", 20), "busy": bool(c.active_job)}

    @app.get("/internal/busy")
    def busy():
        c = controller()
        with c.lock:
            return {"busy": bool(c.active_job)}


app = service_app("capacity", install, tick=lambda ctx: ctx.controller.reconcile())
