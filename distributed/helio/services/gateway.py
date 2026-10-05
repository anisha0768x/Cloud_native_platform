import asyncio
import base64
import json
import os
from pathlib import Path
import time
import httpx
import psutil
from fastapi import Request
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from helio.common import fail, rpc, service_app, uid

PUBLIC = {"/api/auth/setup-status", "/api/auth/setup", "/api/auth/login"}


def destination(path):
    if path.startswith(("/api/auth", "/api/users")) or path in {
        "/api/settings/security",
        "/api/cloud/iam",
    }:
        return "auth"
    if path.startswith(("/api/services", "/api/alerts", "/api/v1/services", "/api/v1/alerts")):
        return "catalog"
    if path.startswith("/api/v1/metrics"):
        return "metrics"
    if path.startswith(("/api/kubernetes", "/api/workload")):
        return "capacity"
    if path.startswith("/api/logs"):
        return "logs"
    if path.startswith("/api/predictions"):
        return "forecast"
    if path.startswith("/api/maintenance"):
        return "maintenance"
    if path.startswith(("/api/v1/notifications", "/api/notifications")):
        return "notifications"
    if path.startswith("/api/cloud/storage"):
        return "storage"
    if path.startswith(("/api/settings", "/api/v1/audit", "/api/backups", "/api/cloud/resilience")):
        return "configuration"
    if path in {"/api/overview", "/api/cloud/network", "/api/cloud/costs"} or path.startswith(
        "/api/events"
    ):
        return "dashboard"
    fail(404, "Unknown API route")


def permission(path, method):
    if path == "/api/v1/audit":
        return "audit"
    if path.startswith("/api/events"):
        return "admin"
    if method in {"GET", "HEAD"}:
        return "operate" if path == "/api/users" else "read"
    if path.startswith("/api/auth"):
        return "read"
    if (
        path.startswith(("/api/users", "/api/settings", "/api/backups"))
        or path == "/api/kubernetes/autoscale"
    ):
        return "admin"
    if path.startswith("/api/cloud/storage"):
        return "storage"
    if path in {"/api/v1/metrics/ingest", "/api/logs/ingest"}:
        return "ingest"
    return "operate"


def install(app):
    def initialize(ctx):
        ctx.process = psutil.Process()
        ctx.process.cpu_percent()

    app.state.initialize = initialize
    # Replace the common private-service middleware with the public gateway boundary.
    app.user_middleware.clear()

    @app.middleware("http")
    async def boundary(request, call_next):
        path = request.url.path
        if (
            path.startswith("/internal/")
            and request.headers.get("x-helio-internal") != os.environ["INTERNAL_SECRET"]
        ):
            return JSONResponse({"detail": "Private gateway endpoint"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse(
                    {"detail": "Cross-origin mutations are not permitted."}, status_code=403
                )
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 11 * 1024 * 1024:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
            if path.startswith("/api/") and time.time() < app.state.ctx.paused_until:
                return JSONResponse(
                    {"detail": "Workspace backup or recovery is in progress; retry shortly."},
                    status_code=503,
                )
        began = time.perf_counter()
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            }
        )
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            if not path.startswith("/api/auth"):
                await asyncio.to_thread(
                    app.state.ctx.db.emit,
                    "requests.observed",
                    {
                        "service_id": "svc-gateway",
                        "occurred": time.time(),
                        "latency": round((time.perf_counter() - began) * 1000, 3),
                        "status": response.status_code,
                        "trace_id": uid("trace"),
                        "bytes": int(response.headers.get("content-length", "0")),
                    },
                )
        return response

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(rest: str, request: Request):
        path = "/api/" + rest
        if path == "/api/openapi.json":
            schemas = await asyncio.gather(
                *[
                    asyncio.to_thread(rpc, name, "/openapi.json")
                    for name in (
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
                ]
            )
            result = {
                "openapi": "3.1.0",
                "info": {"title": "Helio distributed operations API", "version": "3.0.0"},
                "paths": {},
                "components": {"schemas": {}},
            }
            for schema in schemas:
                result["paths"].update(
                    {p: v for p, v in schema["paths"].items() if p.startswith("/api/")}
                )
                result["components"]["schemas"].update(
                    schema.get("components", {}).get("schemas", {})
                )
            return result
        target = destination(path)
        headers = {
            "X-Helio-Internal": os.environ["INTERNAL_SECRET"],
            "X-Helio-Transport": request.url.scheme,
            "X-Helio-Client": request.client.host,
            "X-Helio-Loopback": "true" if request.client.host in {"127.0.0.1", "::1"} else "false",
        }
        if path not in PUBLIC:
            bearer = request.headers.get("authorization", "").startswith("Bearer ")
            token = (
                request.headers["authorization"][7:]
                if bearer
                else request.cookies.get("helio_session", "")
            )
            user = await asyncio.to_thread(
                rpc,
                "auth",
                "/internal/introspect",
                "POST",
                {
                    "token": token,
                    "csrf": request.headers.get("x-csrf-token", ""),
                    "bearer": bearer,
                    "method": request.method,
                    "permission": permission(path, request.method),
                },
            )
            headers["X-Helio-Actor"] = base64.urlsafe_b64encode(json.dumps(user).encode()).decode()
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        chunks = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > 11 * 1024 * 1024:
                fail(413, "Request is too large.")
            chunks.append(chunk)
        body = b"".join(chunks)
        query = ("?" + str(request.url.query)) if request.url.query else ""
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=180 if "/backups" in path else 30
            ) as client:
                response = await client.request(
                    request.method,
                    "http://" + target + ":8000" + path + query,
                    headers=headers,
                    content=body,
                )
        except httpx.HTTPError:
            fail(503, target + " service did not respond; check Connections.")
        allowed = {"content-type", "content-disposition", "set-cookie", "x-content-sha256"}
        return Response(
            response.content,
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k in allowed},
        )

    @app.get("/")
    def home():
        return FileResponse(Path("web/index.html"), headers={"Cache-Control": "no-store"})

    @app.get("/docs")
    def docs():
        return FileResponse(Path("web/api.html"))

    app.mount("/", StaticFiles(directory="web"), name="frontend")


def tick(ctx):
    values = {
        "cpu_percent": ctx.process.cpu_percent() / max(1, psutil.cpu_count() or 1),
        "memory_mb": ctx.process.memory_info().rss / 1024**2,
    }
    for name, value in values.items():
        ctx.db.emit(
            "metrics.observed",
            {
                "id": uid("metric"),
                "service_id": "svc-gateway",
                "name": name,
                "value": round(value, 3),
                "occurred": time.time(),
                "source": "gateway-process",
            },
        )


app = service_app("gateway", install, tick=tick)
