from concurrent.futures import ThreadPoolExecutor
import statistics
from fastapi import Request
from helio.common import NAMES, actor, fail, rpc, service_app, stamp


def install(app):
    @app.get("/api/overview")
    def overview(request: Request):
        actor(request)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [
                pool.submit(rpc, *args)
                for args in [
                    ("catalog", "/internal/summaries"),
                    ("catalog", "/internal/incidents?status=all"),
                    ("capacity", "/internal/state"),
                    ("metrics", "/internal/status"),
                ]
            ]
            catalog, incidents, runtime, status = [job.result() for job in jobs]
        active = [i for i in incidents["alerts"] if i["status"] != "resolved"]
        ack = [i["acknowledged"] - i["opened"] for i in incidents["alerts"] if i["acknowledged"]]
        return {
            "services": catalog["services"],
            "incidents": active,
            "open_alerts": sum(i["status"] == "open" for i in active),
            "active_incidents": len(active),
            "critical": sum(i["severity"] == "critical" for i in active),
            "median_ack_seconds": round(statistics.median(ack), 1) if ack else None,
            "runtime": runtime,
            "collector": status,
            "generated_at": stamp(),
            "data_mode": "measured",
        }

    @app.get("/api/cloud/network")
    def network(request: Request):
        actor(request)

        def check(name):
            try:
                return rpc(name, "/ready", timeout=4, raw=True).json()
            except Exception:
                return {"service": name, "status": "unavailable"}

        with ThreadPoolExecutor(max_workers=12) as pool:
            states = list(pool.map(check, NAMES))
        runtime = rpc("capacity", "/internal/state")
        return {
            "backend": runtime["backend"],
            "runtime": runtime,
            "transport": request.headers.get("x-helio-transport", "http").upper(),
            "services": states,
            "flows": [
                {
                    "from": "Gateway / internal API",
                    "to": s["service"] + " service",
                    "protocol": "HTTP · separate container and database",
                    "status": s["status"],
                }
                for s in states
            ]
            + [
                {
                    "from": "Service transactional outboxes",
                    "to": "Apache Kafka",
                    "protocol": "Acknowledged events · consumer deduplication",
                    "status": "inspect per-service queue status",
                },
                {
                    "from": "Storage service",
                    "to": "S3-compatible object server",
                    "protocol": "S3 API · encrypted versioned objects",
                    "status": "configured",
                },
            ],
            "note": "Twelve independently running application services; PostgreSQL data is owned by separate service roles/databases. Docker is one host, not multi-host high availability.",
        }

    @app.get("/api/cloud/costs")
    def costs(request: Request):
        actor(request)
        rates = rpc("configuration", "/internal/settings")["cost_rates"]
        runtime = rpc("capacity", "/internal/state")
        usage = rpc("storage", "/internal/usage")
        with ThreadPoolExecutor(max_workers=12) as pool:
            database_bytes = sum(
                pool.map(lambda name: rpc(name, "/internal/database-usage")["bytes"], NAMES)
            )
        rows = [
            {
                "name": "Workload capacity (730-hour planning month)",
                "quantity": runtime["observed"] * 730,
                "unit": "replica-hours",
                "rate": rates["worker_hour"],
                "cost": round(runtime["observed"] * 730 * rates["worker_hour"], 6),
            },
            {
                "name": "Uploaded object contents",
                "quantity": usage["bytes"] / 1024**3,
                "unit": "GB-month",
                "rate": rates["storage_gb_month"],
                "cost": round(usage["bytes"] / 1024**3 * rates["storage_gb_month"], 6),
            },
        ]
        rows.append(
            {
                "name": "Service database storage",
                "quantity": database_bytes / 1024**3,
                "unit": "GB-month",
                "rate": rates["database_gb_month"],
                "cost": round(database_bytes / 1024**3 * rates["database_gb_month"], 6),
            }
        )
        return {
            "rates": rates,
            "line_items": rows,
            "total": round(sum(r["cost"] for r in rows), 6),
            "currency": rates["currency"],
            "budget": rates["budget"],
            "mode": "Partial workload, object and database planning estimate; not provider billing",
            "basis": "Excludes control-plane compute, broker, PostgreSQL system databases/WAL, object-store overhead and retained S3 versions, backups and network charges. No cost-saving claim is made.",
        }

    @app.get("/api/events/failures")
    def failures(request: Request):
        actor(request, "admin")
        rows = []
        for name in NAMES:
            rows.extend(
                row | {"service": name} for row in rpc(name, "/internal/event-failures")["events"]
            )
        return {"events": rows}

    @app.post("/api/events/{service}/{event_id}/retry")
    def retry(service: str, event_id: str, request: Request):
        who = actor(request, "admin")
        if service not in NAMES:
            fail(404, "Unknown service")
        return rpc(service, "/internal/event-failures/" + event_id + "/retry", "POST", actor=who)


app = service_app("dashboard", install)
