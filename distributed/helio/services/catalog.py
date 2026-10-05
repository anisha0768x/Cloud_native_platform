from datetime import datetime
import json
import time
from fastapi import Request
from helio.common import actor, fail, rpc, service_app, stamp, uid
from helio.contracts import Registration, IncidentUpdate, METRICS


def consume(ctx, topic, event):
    point = event["data"]
    key = point["service_id"] + ":" + point["name"]
    prior = ctx.db.get("latest", key)
    if not prior or point["occurred"] > prior["occurred"]:
        ctx.db.put("latest", key, point)


def breach_state(prior, point, active, window):
    if not active:
        count = 0
    elif point["id"] == prior.get("last_id"):
        count = prior.get("count", 0)
    elif 0 < point["occurred"] - prior.get("last_occurred", 0) <= window:
        count = prior.get("count", 0) + 1
    else:
        count = 1
    return {"count": count, "last_id": point["id"], "last_occurred": point["occurred"]}


def tick(ctx):
    db = ctx.db
    thresholds = rpc("configuration", "/internal/settings")["thresholds"]
    for point in db.rows("latest", 1000):
        if point["name"] not in {"latency_p95_ms", "error_percent"}:
            continue
        limit = thresholds["latency_ms" if point["name"] == "latency_p95_ms" else "error_percent"]
        key = point["service_id"] + ":" + point["name"]
        if time.time() - point["occurred"] > 30:
            prior = db.get("breaches", key, {"count": 0})
            if prior.get("count"):
                db.put("breaches", key, {"count": 0})
            continue
        active = point["value"] > limit
        with db.tx():
            counter = db.get("breaches", key, {"count": 0})
            counter = breach_state(counter, point, active, thresholds["window_seconds"])
            db.put("breaches", key, counter)
            existing = next(
                (
                    i
                    for i in db.rows(
                        "incidents",
                        1000,
                        {"service_id": point["service_id"], "rule_key": point["name"]},
                    )
                    if i["status"] != "resolved"
                ),
                None,
            )
            evidence = json.dumps(
                {
                    "metric": point["name"],
                    "value": point["value"],
                    "threshold": limit,
                    "collected_at": stamp(point["occurred"]),
                }
            )
            if existing:
                recovered = existing["condition_active"] and not active
                existing.update(condition_active=active, evidence=evidence)
                db.put("incidents", existing["id"], existing)
                if recovered:
                    event_row(
                        db,
                        existing["id"],
                        "collector",
                        "recovery_observed",
                        "Fresh measured evidence is below the threshold.",
                    )
            elif counter["count"] >= thresholds["consecutive_windows"]:
                service = db.get("services", point["service_id"], {"name": point["service_id"]})
                row = {
                    "id": uid("incident"),
                    "service_id": point["service_id"],
                    "service": service["name"],
                    "title": (
                        "Request latency exceeds threshold"
                        if point["name"] == "latency_p95_ms"
                        else "Request errors exceed threshold"
                    ),
                    "severity": "high" if point["name"] == "latency_p95_ms" else "critical",
                    "rule_key": point["name"],
                    "status": "open",
                    "opened": time.time(),
                    "acknowledged": None,
                    "resolved": None,
                    "owner_id": None,
                    "owner_name": None,
                    "condition_active": True,
                    "evidence": evidence,
                    "resolution": None,
                }
                db.put("incidents", row["id"], row)
                event_row(
                    db,
                    row["id"],
                    "collector",
                    "opened",
                    "Consecutive measured evaluations exceeded the configured threshold.",
                )
                db.emit("incidents.opened", row)
                db.audit("collector", "incident.opened", row["id"])


def event_row(db, incident, who, kind, note):
    row = {
        "id": uid("event"),
        "incident_id": incident,
        "actor": who,
        "kind": kind,
        "note": note,
        "created_at": stamp(),
    }
    db.put("events", row["id"], row)


def install(app):
    def db():
        return app.state.ctx.db

    def initialize(ctx):
        for key, name, source in [
            ("svc-gateway", "helio-gateway", "gateway"),
            ("svc-worker", "checkout-api", "managed"),
        ]:
            if not ctx.db.get("services", key):
                ctx.db.put(
                    "services",
                    key,
                    {
                        "id": key,
                        "name": name,
                        "source": source,
                        "environment": "local",
                        "namespace": "helio",
                        "owner_team": "operations",
                        "kind": "api",
                        "created_at": stamp(),
                    },
                )

    app.state.initialize = initialize

    def get(key):
        row = db().get("services", key)
        if not row:
            fail(404, "Service not found")
        return row

    def summary(row):
        metrics = rpc("metrics", "/internal/summary/" + row["id"])
        state = "unknown"
        if metrics["last_seen"]:
            age = time.time() - datetime.fromisoformat(metrics["last_seen"]).timestamp()
            thresholds = rpc("configuration", "/internal/settings")["thresholds"]
            state = (
                "stale"
                if age > 30
                else (
                    "degraded"
                    if (metrics["latency"] or 0) > thresholds["latency_ms"]
                    or (metrics["error_percent"] or 0) > thresholds["error_percent"]
                    else "healthy"
                )
            )
            if (
                state == "healthy"
                and not metrics["request_count"]
                and row["source"] == "registered"
            ):
                state = "receiving telemetry"
        if row["source"] == "managed":
            runtime = rpc("capacity", "/internal/state")
            if not runtime["connected"] or runtime["observed"] == 0:
                state = "unavailable"
        return row | metrics | {"status": state}

    @app.get("/internal/services/{service_id}")
    def internal_service(service_id: str):
        return get(service_id)

    @app.get("/internal/services")
    def internal_services():
        return {"services": db().rows("services")}

    @app.get("/api/services")
    @app.get("/internal/summaries")
    def services():
        return {"services": [summary(s) for s in db().rows("services")]}

    @app.post("/api/services", status_code=201)
    def register(payload: Registration, request: Request):
        user = actor(request, "operate")
        with db().tx():
            if db().rows("services", 1, {"name": payload.name}):
                fail(409, "Service name is already registered.")
            row = payload.model_dump() | {
                "id": uid("svc"),
                "source": "registered",
                "created_at": stamp(),
            }
            db().put("services", row["id"], row)
            db().emit("services.registered", row)
            db().audit(user["email"], "service.registered", row["id"])
        return row | {"status": "unknown"}

    @app.get("/api/services/{service_id}")
    def detail(service_id: str, request: Request):
        actor(request)
        return {
            "service": summary(get(service_id)),
            "incidents": db().rows("incidents", 100, {"service_id": service_id}),
            "metrics": sorted(METRICS),
        }

    @app.get("/api/alerts")
    @app.get("/internal/incidents")
    def incidents(status: str = "active"):
        rows = db().rows("incidents", 200)
        return {"alerts": [r for r in rows if status != "active" or r["status"] != "resolved"]}

    @app.get("/internal/incidents/{incident_id}")
    def incident(incident_id: str):
        row = db().get("incidents", incident_id)
        if not row:
            fail(404, "Incident not found")
        return row

    @app.get("/api/alerts/{incident_id}")
    def detail_incident(incident_id: str, request: Request):
        actor(request)
        return {
            "incident": incident(incident_id),
            "events": sorted(
                db().rows("events", 500, {"incident_id": incident_id}),
                key=lambda e: e["created_at"],
            ),
        }

    def transition(key, who, payload):
        owner = rpc("auth", "/internal/users/" + (payload.owner_id or who["id"]))
        if not owner["active"] or owner["role"] == "viewer":
            fail(422, "Assign an active operator or administrator.")
        with db().tx():
            row = incident(key)
            allowed = {
                "open": ["open", "acknowledged"],
                "acknowledged": ["acknowledged", "investigating"],
                "investigating": ["investigating", "resolved"],
                "resolved": [],
            }
            if payload.status not in allowed[row["status"]]:
                fail(409, "Invalid incident transition.")
            if payload.status == "resolved":
                evidence = json.loads(row["evidence"])
                if (
                    row["condition_active"]
                    or time.time() - datetime.fromisoformat(evidence["collected_at"]).timestamp()
                    > 60
                ):
                    fail(409, "Fresh below-threshold recovery evidence is required.")
            row.update(status=payload.status, owner_id=owner["id"], owner_name=owner["name"])
            if payload.status != "open" and not row["acknowledged"]:
                row["acknowledged"] = time.time()
            if payload.status == "resolved":
                row.update(resolved=time.time(), resolution=payload.note)
            db().put("incidents", key, row)
            event_row(db(), key, who["email"], payload.status, payload.note)
            db().audit(who["email"], "incident." + payload.status, key)
        return row

    @app.post("/api/alerts/{incident_id}/acknowledge")
    def ack(incident_id: str, request: Request):
        who = actor(request, "operate")
        row = incident(incident_id)
        if row["status"] != "open":
            return row
        return transition(
            incident_id,
            who,
            IncidentUpdate(
                status="acknowledged", note="Acknowledged and assigned to the signed-in operator."
            ),
        )

    @app.patch("/api/alerts/{incident_id}")
    def change(incident_id: str, payload: IncidentUpdate, request: Request):
        return transition(incident_id, actor(request, "operate"), payload)


app = service_app("catalog", install, ["metrics.observed"], consume, tick)
