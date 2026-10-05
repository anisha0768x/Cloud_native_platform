from datetime import datetime
import math
import time
from fastapi import Request, Query
from helio.common import actor, fail, rpc, service_app, stamp, uid
from helio.contracts import Metric


def p95(values):
    return round(sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)], 2) if values else None


def consume(ctx, topic, event):
    data = event["data"]
    if topic == "requests.observed":
        ctx.db.put("requests", event["id"], data | {"id": event["id"]})
    elif event["source"] != "metrics":
        ctx.db.put("metrics", data["id"], data)


def tick(ctx):
    now = time.time()
    db = ctx.db
    thresholds = rpc("configuration", "/internal/settings")["thresholds"]
    window = thresholds["window_seconds"]
    requests = [
        r["body"]
        for r in db.sql(
            "SELECT body FROM documents WHERE collection='requests' AND (body->>'occurred')::double precision>=%s ORDER BY (body->>'occurred')::double precision DESC LIMIT 10000",
            (now - window,),
        )
    ]
    grouped = {}
    for row in requests:
        grouped.setdefault(row["service_id"], []).append(row)
    grouped.setdefault("svc-worker", [])
    grouped.setdefault("svc-gateway", [])
    for service, rows in grouped.items():
        values = {"request_rate": round(len(rows) / window, 3)}
        if rows:
            values.update(
                latency_p95_ms=p95([r["latency"] for r in rows]),
                error_percent=round(sum(r["status"] >= 400 for r in rows) / len(rows) * 100, 2),
            )
        with db.tx():
            for name, value in values.items():
                point = {
                    "id": uid("metric"),
                    "service_id": service,
                    "name": name,
                    "value": value,
                    "occurred": now,
                    "source": "measured-requests",
                }
                db.put("metrics", point["id"], point)
                db.emit("metrics.observed", point)
    # Bounded retention, with SQL pruning in the owning service only.
    db.sql(
        "DELETE FROM documents WHERE collection IN ('metrics','requests') AND updated<%s",
        (now - 7 * 86400,),
    )
    db.put("status", "collection", {"last_success": stamp(), "error": None})


def install(app):
    def db():
        return app.state.ctx.db

    @app.post("/api/v1/metrics/ingest", status_code=201)
    def ingest(payload: Metric, request: Request):
        user = actor(request, "ingest")
        rpc("catalog", "/internal/services/" + payload.service_id)
        occurred = time.time()
        if payload.occurred_at:
            try:
                parsed = datetime.fromisoformat(payload.occurred_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                occurred = parsed.timestamp()
            except ValueError:
                fail(422, "occurred_at must include a timezone.")
            if not time.time() - 7 * 86400 <= occurred <= time.time() + 30:
                fail(422, "Observation timestamp is outside the accepted range.")
        if payload.metric_name.endswith("percent") and payload.value > 100:
            fail(422, "Percentage must be between 0 and 100.")
        row = {
            "id": uid("metric"),
            "service_id": payload.service_id,
            "name": payload.metric_name,
            "value": payload.value,
            "occurred": occurred,
            "source": "agent:" + user["email"],
        }
        with db().tx():
            db().put("metrics", row["id"], row)
            db().emit("metrics.observed", row)
        return {"accepted": True, "id": row["id"]}

    @app.get("/api/v1/metrics/query")
    def query(
        request: Request,
        service_id: str = "svc-worker",
        metric_name: str = "request_rate",
        minutes: int = Query(15, ge=1, le=10080),
    ):
        actor(request)
        return {
            "service_id": service_id,
            "metric_name": metric_name,
            "points": history(service_id, metric_name, minutes)["points"],
            "source": "persisted PostgreSQL observations",
            "window_minutes": minutes,
        }

    @app.get("/internal/history")
    def history(
        service_id: str = "svc-worker", metric_name: str = "request_rate", minutes: int = 15
    ):
        rows = db().rows(
            "metrics",
            600,
            {"service_id": service_id, "name": metric_name},
            time.time() - min(minutes, 10080) * 60,
        )
        return {
            "points": sorted(
                [r for r in rows if r["occurred"] >= time.time() - min(minutes, 10080) * 60],
                key=lambda x: x["occurred"],
            )
        }

    @app.get("/api/v1/metrics/latest")
    def latest(request: Request, service_id: str, metric_name: str):
        actor(request)
        rows = db().rows("metrics", 600, {"service_id": service_id, "name": metric_name})
        return (
            max(rows, key=lambda r: r["occurred"])
            if rows
            else {"value": None, "status": "awaiting_telemetry"}
        )

    @app.get("/internal/summary/{service_id}")
    def summary(service_id: str):
        now = time.time()
        rows = [
            r
            for r in db().rows("requests", 10000, {"service_id": service_id}, now - 300)
            if r["occurred"] >= now - 300
        ]
        points = [
            r
            for r in db().rows("metrics", 500, {"service_id": service_id}, now - 300)
            if r["occurred"] >= now - 300
        ]
        latest = {}
        for p in sorted(points, key=lambda x: x["occurred"], reverse=True):
            latest.setdefault(p["name"], p)
        latency = (
            p95([r["latency"] for r in rows])
            if rows
            else latest.get("latency_p95_ms", {}).get("value")
        )
        errors = (
            round(sum(r["status"] >= 400 for r in rows) / len(rows) * 100, 2)
            if rows
            else latest.get("error_percent", {}).get("value")
        )
        last = max([r["occurred"] for r in rows] + [p["occurred"] for p in points] + [0])
        limit = rpc("configuration", "/internal/settings")["thresholds"]["latency_ms"]
        return {
            "request_count": len(rows),
            "latency": latency,
            "error_percent": errors,
            "success_percent": 100 - errors if rows else None,
            "slo_good_percent": (
                round(
                    sum(r["status"] < 400 and r["latency"] <= limit for r in rows)
                    / len(rows)
                    * 100,
                    2,
                )
                if rows
                else None
            ),
            "rps": latest.get("request_rate", {}).get("value", 0),
            "last_seen": stamp(last) if last else None,
            "latest": latest,
        }

    @app.get("/internal/status")
    def status():
        return db().get(
            "status", "collection", {"last_success": None, "error": "Awaiting first collection"}
        ) | {"error": app.state.ctx.tick_error}


app = service_app("metrics", install, ["requests.observed", "metrics.observed"], consume, tick)
