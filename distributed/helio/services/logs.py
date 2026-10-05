import re
import time
from fastapi import Request, Query
from helio.common import actor, rpc, service_app, stamp, uid
from helio.contracts import LogAnalysisRequest, LogInput


def redact(value):
    value = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email redacted]", value, flags=re.I)
    value = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[redacted]", value)
    return re.sub(
        r"(?i)((?:password|secret|api[_-]?key|token)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", value
    )[:10000]


def consume(ctx, topic, event):
    r = event["data"]
    ctx.db.put(
        "logs",
        event["id"],
        {
            "id": event["id"],
            "service_id": r["service_id"],
            "service": r.get("service", r["service_id"]),
            "occurred": r["occurred"],
            "level": "ERROR" if r["status"] >= 400 else "WARN" if r["latency"] > 350 else "INFO",
            "message": f"HTTP {r['status']}; completed in {r['latency']:.1f} ms; {r.get('bytes',0)} response bytes",
            "trace_id": r.get("trace_id"),
            "source": "measured-request",
        },
    )


def install(app):
    def db():
        return app.state.ctx.db

    @app.get("/api/logs")
    @app.get("/internal/logs")
    def logs(
        service_id: str | None = None,
        level: str | None = None,
        query: str = Query("", max_length=200),
        limit: int = Query(100, ge=1, le=500),
    ):
        match = {}
        if service_id:
            match["service_id"] = service_id
        if level:
            match["level"] = level
        rows = db().rows("logs", 2000, match)
        return {
            "entries": [r for r in rows if query.lower() in r["message"].lower()][:limit],
            "redaction": "Email and credential-pattern redaction; not complete PII detection.",
        }

    @app.post("/api/logs/ingest", status_code=201)
    def ingest(payload: LogInput, request: Request):
        who = actor(request, "ingest")
        service = rpc("catalog", "/internal/services/" + payload.service_id)
        row = payload.model_dump() | {
            "id": uid("log"),
            "message": redact(payload.message),
            "service": service["name"],
            "occurred": time.time(),
            "source": "agent:" + who["email"],
        }
        db().put("logs", row["id"], row)
        return {"id": row["id"]}

    @app.post("/api/logs/analyze")
    def analyze(payload: LogAnalysisRequest, request: Request):
        who = actor(request, "operate")
        service = payload.service_id
        rpc("catalog", "/internal/services/" + service)
        rows = db().rows("logs", 100, {"service_id": service}, time.time() - 900)
        selected = [
            r
            for r in rows
            if r["level"] in {"WARN", "ERROR"}
            or payload.query
            and payload.query.lower() in r["message"].lower()
        ][:12]
        metrics = rpc("metrics", "/internal/summary/" + service)["latest"]
        points = list(metrics.values())[:10]
        findings = []
        for title, pattern, advice in [
            (
                "Dependency timeout",
                r"timeout|timed out",
                "Inspect the named dependency and compare measured latency.",
            ),
            (
                "Memory exhaustion",
                r"out of memory|oom|memory.*limit",
                "Inspect memory limits and allocation failures.",
            ),
            (
                "Connection pool contention",
                r"pool.*exhaust|connection.*pool",
                "Inspect pool occupancy and slow queries.",
            ),
        ]:
            ids = [r["id"] for r in selected if re.search(pattern, r["message"], re.I)]
            if ids:
                findings.append({"title": title, "recommendation": advice, "log_ids": ids})
        result = {
            "method": "Evidence-linked diagnostic rules",
            "summary": (
                findings[0]["title"] if findings else "Insufficient evidence to identify a cause"
            ),
            "findings": findings,
            "logs": selected,
            "metrics": points,
            "incidents": [],
            "sources": len(selected) + len(points),
            "confidence": None,
            "redaction": "Email and credential-pattern redaction",
            "generated_at": stamp(),
        }
        db().put("analyses", uid("analysis"), result)
        db().audit(who["email"], "logs.analyzed", service, {"sources": result["sources"]})
        return result


def tick(ctx):
    ctx.db.sql(
        "DELETE FROM documents WHERE collection='logs' AND updated<%s", (time.time() - 7 * 86400,)
    )


app = service_app("logs", install, ["requests.observed"], consume, tick)
