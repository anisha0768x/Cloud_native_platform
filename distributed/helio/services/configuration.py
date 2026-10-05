import json
import threading
import time
from fastapi import Request, Query
from helio.common import NAMES, DEFAULT_THRESHOLDS, actor, fail, rpc, service_app, stamp
from helio.contracts import Thresholds, Rates

RATES = {
    "worker_hour": 0.02,
    "storage_gb_month": 0.023,
    "database_gb_month": 0.1,
    "budget": 20,
    "currency": "USD",
}


def consume(ctx, topic, event):
    ctx.db.put("audit", event["data"]["id"], event["data"])


def install(app):
    operation_lock = threading.Lock()

    def db():
        return app.state.ctx.db

    def settings():
        return {
            "thresholds": db().get("settings", "thresholds", DEFAULT_THRESHOLDS),
            "cost_rates": db().get("settings", "cost_rates", RATES),
            "retention": {
                "telemetry_days": 7,
                "audit": "No automatic deletion",
                "objects": "Enforced per version by storage service",
            },
        }

    def pause(seconds):
        for name in NAMES:
            if name != "configuration":
                rpc(name, "/internal/pause", "POST", {"seconds": seconds}, timeout=30)

    def unpause():
        for name in NAMES:
            if name != "configuration":
                try:
                    rpc(name, "/internal/pause", "POST", {"seconds": 0}, timeout=3)
                except Exception:
                    pass

    def create_backup(who):
        if rpc("capacity", "/internal/busy")["busy"]:
            fail(409, "Wait for the active workload test to finish.")
        snapshots = []
        for name in NAMES:
            snapshots.append(
                rpc(name, "/internal/snapshot", timeout=20)
                if name != "configuration"
                else {
                    "service": name,
                    "records": db().sql(
                        "SELECT collection,id,body FROM documents WHERE collection NOT IN ('backups')"
                    ),
                }
            )
        row = rpc("storage", "/internal/archive", "POST", {"snapshots": snapshots}, timeout=120)
        db().put("backups", row["id"], row)
        db().audit(who, "backup.created", row["id"])
        return row

    def backup_row(key):
        row = db().get("backups", key)
        if not row:
            fail(404, "Backup not found")
        return row

    @app.get("/internal/settings")
    def internal_settings():
        return settings()

    @app.get("/api/settings")
    def public_settings(request: Request):
        actor(request)
        return settings() | {"security": rpc("auth", "/internal/security")}

    @app.put("/api/settings/thresholds")
    def thresholds(payload: Thresholds, request: Request):
        who = actor(request, "admin")
        with db().tx():
            db().put("settings", "thresholds", payload.model_dump())
            db().audit(who["email"], "thresholds.updated", "workspace", payload.model_dump())
        return payload

    @app.put("/api/settings/costs")
    def rates(payload: Rates, request: Request):
        who = actor(request, "admin")
        with db().tx():
            db().put("settings", "cost_rates", payload.model_dump())
            db().audit(who["email"], "rates.updated", "workspace", payload.model_dump())
        return payload

    @app.get("/api/v1/audit")
    def audit(request: Request, query: str = Query("", max_length=100)):
        actor(request, "audit")
        rows = db().rows("audit", 1000)
        return {
            "events": [
                r
                for r in rows
                if query.lower() in (r["actor"] + " " + r["action"] + " " + r["resource"]).lower()
            ][:200]
        }

    @app.get("/api/cloud/resilience")
    def resilience(request: Request):
        actor(request)
        last_restore = db().get("recovery", "last_success")
        return {
            "backups": db().rows("backups", 100),
            "method": "Encrypted service-owned PostgreSQL document snapshots and object contents in S3-compatible storage",
            "schedule": "Manual, with ingress paused during capture",
            "rpo": "Time since the selected backup; no cross-service point-in-time or off-site guarantee",
            "rto": (
                f"Last completed restore: {last_restore['seconds']} seconds; no production RTO guarantee"
                if last_restore
                else "No completed restore has been measured in this workspace; no production RTO guarantee"
            ),
        }

    @app.post("/api/backups", status_code=201)
    def backup(request: Request):
        who = actor(request, "admin")
        if not operation_lock.acquire(blocking=False):
            fail(409, "Another recovery operation is active.")
        try:
            pause(180)
            return create_backup(who["email"])
        finally:
            unpause()
            operation_lock.release()

    @app.post("/api/backups/{key}/verify")
    def verify(key: str, request: Request):
        who = actor(request, "admin")
        row = backup_row(key)
        result = rpc("storage", "/internal/archive/verify", "POST", row, timeout=120)
        row.update(verified_at=stamp(), verify_seconds=result["seconds"])
        db().put("backups", key, row)
        db().audit(who["email"], "backup.verified", key)
        return result

    @app.post("/api/backups/{key}/restore")
    def restore(key: str, payload: dict, request: Request):
        started = time.perf_counter()
        who = actor(request, "admin")
        if payload.get("confirmation") != key:
            fail(403, "Enter the backup identifier to confirm restoration.")
        rpc(
            "auth",
            "/internal/reauth",
            "POST",
            {"id": who["id"], "password": payload.get("password", "")},
        )
        if rpc("capacity", "/internal/busy")["busy"]:
            fail(409, "A workload test is running.")
        row = backup_row(key)
        rpc("storage", "/internal/archive/verify", "POST", row, timeout=120)
        if not operation_lock.acquire(blocking=False):
            fail(409, "Another recovery operation is active.")
        try:
            pause(300)
            safety = create_backup(who["email"])
            manifest = rpc("storage", "/internal/archive/read", "POST", row, timeout=120)
            event_floor = time.time()
            restored = []
            try:
                for snapshot in manifest["snapshots"]:
                    name = snapshot["service"]
                    if name == "configuration":
                        with db().tx():
                            db().sql("DELETE FROM documents WHERE collection!='backups'")
                            db().sql("DELETE FROM outbox WHERE sent IS NULL")
                            for item in snapshot["records"]:
                                if item["collection"] != "backups":
                                    db().put(item["collection"], item["id"], item["body"])
                            db().put("_system", "event_floor", {"time": event_floor})
                    else:
                        rpc(
                            name,
                            "/internal/restore",
                            "POST",
                            snapshot | {"event_floor": event_floor},
                            timeout=30,
                        )
                    restored.append(name)
                rpc("storage", "/internal/archive/restore-objects", "POST", row, timeout=120)
                db().audit(
                    who["email"],
                    "backup.restored",
                    key,
                    {"safety_backup": safety["id"], "services": restored},
                )
                db().put(
                    "recovery",
                    "last_success",
                    {
                        "id": key,
                        "time": stamp(),
                        "seconds": round(time.perf_counter() - started, 3),
                    },
                )
            except Exception:
                db().put(
                    "recovery",
                    "last_failure",
                    {"safety_backup": safety["id"], "restored_services": restored, "time": stamp()},
                )
                fail(
                    503,
                    "Restore was interrupted. Safety backup "
                    + safety["id"]
                    + " is retained; check service health before retrying.",
                )
            return {
                "status": "restored",
                "safety_backup": safety["id"],
                "services": restored,
                "message": "Service data restored. All browser sessions were revoked.",
            }
        finally:
            unpause()
            operation_lock.release()


app = service_app("configuration", install, ["audit.events"], consume)
