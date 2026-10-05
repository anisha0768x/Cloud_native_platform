"""Measured failure-evidence collection; no invented failure probability."""

import time
from fastapi import Request
from helio.common import actor, fail, rpc, service_app, stamp, uid
from helio.contracts import MaintenanceOutcome


def tick(ctx):
    last = ctx.db.get("state", "sample", {"time": 0})
    if time.time() - last["time"] < 60:
        return
    summary = rpc("metrics", "/internal/summary/svc-worker")
    runtime = rpc("capacity", "/internal/state")
    row = {
        "id": uid("observation"),
        "service_id": "svc-worker",
        "occurred": time.time(),
        "features": {
            "latency_p95_ms": summary["latency"],
            "error_percent": summary["error_percent"],
            "request_rate": summary["rps"],
            "ready_replicas": runtime["observed"],
        },
        "failed_within_five_minutes": None,
    }
    ctx.db.put("observations", row["id"], row)
    ctx.db.put("state", "sample", {"time": time.time()})


def install(app):
    def db():
        return app.state.ctx.db

    @app.get("/api/maintenance")
    def maintenance(request: Request):
        actor(request)
        rows = db().rows("observations", 500)
        labelled = [r for r in rows if r["failed_within_five_minutes"] is not None]
        runtime = rpc("capacity", "/internal/state")
        return {
            "method": "Measured health rules and labelled outcome collection",
            "model_status": "not_trained",
            "failure_probability": None,
            "reason": "No validated failure-prediction model is deployed; collect representative labelled failures before claiming predictive accuracy.",
            "observations": rows[:30],
            "labelled_samples": len(labelled),
            "failure_samples": sum(r["failed_within_five_minutes"] for r in labelled),
            "current_health": "healthy" if runtime["workload_connected"] else "unavailable",
            "last_checked": stamp(),
        }

    @app.post("/api/maintenance/outcomes")
    def label(payload: MaintenanceOutcome, request: Request):
        who = actor(request, "operate")
        row = db().get("observations", payload.observation_id)
        if not row:
            fail(404, "Observation not found")
        if time.time() - row["occurred"] < 300:
            fail(409, "Wait until the full five-minute outcome window has elapsed.")
        row.update(
            failed_within_five_minutes=payload.failed,
            label_evidence=payload.evidence,
            label_actor=who["email"],
        )
        db().put("observations", row["id"], row)
        db().audit(who["email"], "maintenance.outcome_recorded", row["id"])
        return row


app = service_app("maintenance", install, tick=tick)
