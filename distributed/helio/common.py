"""Transport and persistence primitives; no cross-service database access."""

from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from confluent_kafka import Producer, Consumer, KafkaException

NAMES = (
    "gateway",
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
PERMISSIONS = {
    "viewer": {"read"},
    "operator": {"read", "operate", "ingest", "storage", "audit"},
    "admin": {"read", "operate", "ingest", "storage", "audit", "admin"},
}
DEFAULT_THRESHOLDS = {
    "latency_ms": 350,
    "error_percent": 5,
    "consecutive_windows": 2,
    "window_seconds": 30,
}


def stamp(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).isoformat(
        timespec="milliseconds"
    )


def uid(prefix):
    return prefix + "-" + secrets.token_hex(12)


def fail(status, detail):
    raise HTTPException(status, detail)


class Database:
    def __init__(self):
        self.pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self.pool.wait(timeout=45)
        self.lock = threading.RLock()
        self.local = threading.local()
        with self.pool.connection() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS documents(collection TEXT NOT NULL,id TEXT NOT NULL,body JSONB NOT NULL,updated DOUBLE PRECISION NOT NULL,PRIMARY KEY(collection,id));
CREATE INDEX IF NOT EXISTS documents_time ON documents(collection,updated DESC);
CREATE INDEX IF NOT EXISTS documents_body ON documents USING GIN(body);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,topic TEXT NOT NULL,payload JSONB NOT NULL,created DOUBLE PRECISION NOT NULL,sent DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(created) WHERE sent IS NULL;
CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,received DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS dead_letters(id TEXT PRIMARY KEY,payload JSONB NOT NULL,error TEXT NOT NULL,created DOUBLE PRECISION NOT NULL);
ALTER TABLE dead_letters ADD COLUMN IF NOT EXISTS topic TEXT;"""
            )

    @contextmanager
    def tx(self):
        if getattr(self.local, "con", None) is not None:
            yield self.local.con
            return
        with self.lock, self.pool.connection() as con:
            self.local.con = con
            try:
                with con.transaction():
                    yield con
            finally:
                self.local.con = None

    def sql(self, query, args=(), one=False):
        with self.tx() as con:
            cur = con.execute(query, args)
            if cur.description:
                return cur.fetchone() if one else cur.fetchall()
            return cur.rowcount

    def get(self, collection, key, default=None):
        row = self.sql(
            "SELECT body FROM documents WHERE collection=%s AND id=%s", (collection, key), one=True
        )
        return row["body"] if row else default

    def put(self, collection, key, body):
        self.sql(
            "INSERT INTO documents VALUES(%s,%s,%s,%s) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body,updated=excluded.updated",
            (collection, key, Jsonb(body), time.time()),
        )
        return body

    def delete(self, collection, key):
        return self.sql("DELETE FROM documents WHERE collection=%s AND id=%s", (collection, key))

    def rows(self, collection, limit=1000, match=None, since=0):
        return [
            r["body"]
            for r in self.sql(
                "SELECT body FROM documents WHERE collection=%s AND body @> %s AND updated>=%s ORDER BY updated DESC LIMIT %s",
                (collection, Jsonb(match or {}), since, limit),
            )
        ]

    def emit(self, topic, payload, event_id=None):
        event = {
            "id": event_id or uid("event"),
            "source": os.environ["HELIO_SERVICE"],
            "time": time.time(),
            "data": payload,
            "schema": 1,
        }
        self.sql(
            "INSERT INTO outbox VALUES(%s,%s,%s,%s,NULL) ON CONFLICT DO NOTHING",
            (event["id"], topic, Jsonb(event), time.time()),
        )
        return event["id"]

    def audit(self, actor, action, resource, detail=None):
        self.emit(
            "audit.events",
            {
                "id": uid("audit"),
                "actor": actor,
                "action": action,
                "resource": resource,
                "detail": json.dumps(detail or {}),
                "created_at": stamp(),
            },
        )


def rpc(service, path, method="GET", body=None, actor=None, timeout=8, raw=False):
    headers = {"X-Helio-Internal": os.environ["INTERNAL_SECRET"]}
    if actor:
        headers["X-Helio-Actor"] = base64.urlsafe_b64encode(json.dumps(actor).encode()).decode()
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.request(
                method, "http://" + service + ":8000" + path, headers=headers, json=body
            )
    except httpx.HTTPError:
        fail(503, service + " service is unavailable; retry after its connection recovers.")
    if raw:
        return response
    if not response.is_success:
        try:
            detail = response.json().get("detail", "Dependency request failed")
        except ValueError:
            detail = "Dependency request failed"
        fail(response.status_code, detail)
    return response.json()


def actor(request, permission="read"):
    try:
        user = json.loads(base64.urlsafe_b64decode(request.headers.get("x-helio-actor", "")))
    except (ValueError, TypeError):
        fail(401, "An authenticated identity is required.")
    if permission not in PERMISSIONS.get(user.get("role"), set()):
        fail(403, "Your role cannot perform this action.")
    return user


class Events:
    def __init__(self, ctx, topics, handler):
        self.ctx, self.topics, self.handler = ctx, topics, handler
        self.producer = Producer(
            {
                "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
                "message.timeout.ms": 5000,
                "enable.idempotence": True,
                "acks": "all",
                "log_level": 0,
            }
        )
        self.consumer = None
        self.last_publish = None
        self.last_consume = None
        self.error = None

    def publish(self):
        rows = self.ctx.db.sql(
            "SELECT id,topic,payload FROM outbox WHERE sent IS NULL ORDER BY created LIMIT 100"
        )
        if not rows:
            return
        delivered = []
        for row in rows:

            def confirm(error, message, key=row["id"]):
                if error is None:
                    delivered.append(key)

            self.producer.produce(
                row["topic"],
                key=str(row["payload"]["data"].get("service_id", row["id"])),
                value=json.dumps(row["payload"]).encode(),
                on_delivery=confirm,
            )
        self.producer.flush(6)
        with self.ctx.db.tx():
            for key in delivered:
                self.ctx.db.sql("UPDATE outbox SET sent=%s WHERE id=%s", (time.time(), key))
        if len(delivered) != len(rows):
            raise RuntimeError(
                "Kafka acknowledgement unavailable; unacknowledged outbox entries retained"
            )
        self.last_publish = stamp()

    def consume(self):
        if not self.topics:
            return
        if self.consumer is None:
            self.consumer = Consumer(
                {
                    "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
                    "group.id": "helio-v3-" + self.ctx.name,
                    "auto.offset.reset": "earliest",
                    "enable.auto.commit": False,
                    "log_level": 0,
                }
            )
            self.consumer.subscribe(self.topics)
        message = self.consumer.poll(0.15)
        if not message:
            return
        if message.error():
            raise KafkaException(message.error())
        try:
            event = json.loads(message.value())
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("id"), str)
                or not isinstance(event.get("data"), dict)
                or event.get("schema") != 1
            ):
                raise ValueError("Invalid event envelope")
        except (ValueError, TypeError):
            event = {"id": hashlib.sha256(message.value()).hexdigest(), "data": {}, "invalid": True}
        if (
            not event.get("invalid")
            and event.get("time", 0)
            < self.ctx.db.get("_system", "event_floor", {"time": 0})["time"]
        ):
            self.consumer.commit(message=message, asynchronous=False)
            return
        if self.ctx.db.sql("SELECT id FROM inbox WHERE id=%s", (event["id"],), one=True):
            self.consumer.commit(message=message, asynchronous=False)
            return
        for attempt in range(3):
            try:
                with self.ctx.db.tx():
                    if event.get("invalid"):
                        raise ValueError("Malformed event JSON")
                    self.handler(self.ctx, message.topic(), event)
                    self.ctx.db.sql(
                        "INSERT INTO inbox VALUES(%s,%s) ON CONFLICT DO NOTHING",
                        (event["id"], time.time()),
                    )
                break
            except Exception as exc:
                if attempt == 2:
                    self.ctx.db.sql(
                        "INSERT INTO dead_letters(id,payload,error,created,topic) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (
                            event["id"],
                            Jsonb(event),
                            type(exc).__name__,
                            time.time(),
                            message.topic(),
                        ),
                    )
                    self.ctx.db.emit(
                        "events.failed",
                        {
                            "event_id": event["id"],
                            "consumer": self.ctx.name,
                            "reason": type(exc).__name__,
                        },
                    )
                else:
                    time.sleep(0.2 * (attempt + 1))
        self.consumer.commit(message=message, asynchronous=False)
        self.last_consume = stamp()


class Context:
    def __init__(self, name):
        self.name = name
        self.db = Database()
        self.stop = threading.Event()
        self.threads = []
        self.paused_until = 0
        self.tick_error = None
        self.last_tick = None
        self.workers = {}
        self.activity = threading.Condition()
        self.active_steps = 0

    def background(self, fn, interval, label):
        def loop():
            while not self.stop.wait(interval):
                with self.activity:
                    if time.time() < self.paused_until:
                        continue
                    self.active_steps += 1
                try:
                    fn()
                    self.workers[label] = {"last_success": stamp(), "error": None}
                    if label == "collection":
                        self.tick_error = None
                        self.last_tick = stamp()
                except Exception as exc:
                    self.workers[label] = {
                        "last_success": self.workers.get(label, {}).get("last_success"),
                        "error": type(exc).__name__,
                    }
                    if label == "collection":
                        self.tick_error = type(exc).__name__
                        self.last_tick = self.workers[label]["last_success"]
                    logging.warning("%s %s step failed: %s", self.name, label, type(exc).__name__)
                finally:
                    with self.activity:
                        self.active_steps -= 1
                        self.activity.notify_all()

        thread = threading.Thread(target=loop, daemon=True)
        self.threads.append(thread)
        thread.start()


def service_app(name, install, topics=(), handler=None, tick=None):
    @asynccontextmanager
    async def lifespan(app):
        ctx = Context(name)
        app.state.ctx = ctx
        ctx.events = Events(ctx, topics, handler)
        if hasattr(app.state, "initialize"):
            app.state.initialize(ctx)
        # Kafka publishing and consuming are independent of application collection/delivery.
        ctx.background(ctx.events.publish, 0.3, "publisher")
        if topics:
            ctx.background(ctx.events.consume, 0.01, "consumer")
        if tick:
            ctx.background(lambda: tick(ctx), 2, "collection")

        def prune_events():
            ctx.db.sql("DELETE FROM outbox WHERE sent<%s", (time.time() - 8 * 86400,))
            ctx.db.sql("DELETE FROM inbox WHERE received<%s", (time.time() - 8 * 86400,))

        ctx.background(prune_events, 600, "event-retention")
        yield
        ctx.stop.set()
        for thread in ctx.threads:
            thread.join(timeout=8)
        if ctx.events.consumer:
            ctx.events.consumer.close()
        if hasattr(ctx, "close"):
            ctx.close()
        ctx.db.pool.close()

    app = FastAPI(
        title="Helio " + name, version="3.0.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    @app.middleware("http")
    async def internal_boundary(request, call_next):
        if request.url.path not in {"/health", "/ready"}:
            if not hmac.compare_digest(
                request.headers.get("x-helio-internal", ""), os.environ["INTERNAL_SECRET"]
            ):
                return JSONResponse({"detail": "Private service endpoint."}, status_code=403)
            if (
                time.time() < app.state.ctx.paused_until
                and request.method not in {"GET", "HEAD"}
                and not request.url.path.startswith("/internal/")
            ):
                return JSONResponse(
                    {"detail": "Workspace recovery is in progress."}, status_code=503
                )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        return JSONResponse(
            {
                "detail": "; ".join(
                    ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in error.errors()
                )
            },
            status_code=422,
        )

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def duplicate(request, error):
        return JSONResponse(
            {"detail": "A record with this identity already exists."}, status_code=409
        )

    @app.get("/health")
    def health():
        return {"service": name, "status": "ok", "version": "3.0.0"}

    @app.get("/ready")
    def ready():
        ctx = app.state.ctx
        try:
            ctx.db.sql("SELECT 1")
            if hasattr(app.state, "check_dependency"):
                app.state.check_dependency(ctx)
            pending = ctx.db.sql(
                "SELECT count(*) AS n,min(created) AS oldest FROM outbox WHERE sent IS NULL",
                one=True,
            )
            dead = ctx.db.sql("SELECT count(*) AS n FROM dead_letters", one=True)["n"]
            errors = {k: v["error"] for k, v in ctx.workers.items() if v["error"]}
            delayed = pending["oldest"] is not None and time.time() - pending["oldest"] > 30
            body = {
                "service": name,
                "status": "degraded" if errors or delayed or dead else "ready",
                "database": "PostgreSQL",
                "outbox_pending": pending["n"],
                "dead_letters": dead,
                "oldest_pending": pending["oldest"],
                "last_tick": ctx.last_tick,
                "background_error": ctx.tick_error,
                "workers": ctx.workers,
                "last_published": ctx.events.last_publish,
                "last_consumed": ctx.events.last_consume,
            }
            return JSONResponse(body, status_code=503 if errors or delayed or dead else 200)
        except Exception:
            return JSONResponse({"service": name, "status": "unavailable"}, status_code=503)

    @app.get("/internal/snapshot")
    def snapshot():
        return {
            "service": name,
            "records": app.state.ctx.db.sql(
                "SELECT collection,id,body FROM documents WHERE collection NOT IN ('sessions','attempts')"
            ),
        }

    @app.get("/internal/database-usage")
    def database_usage():
        return {
            "bytes": app.state.ctx.db.sql(
                "SELECT pg_database_size(current_database()) AS bytes", one=True
            )["bytes"]
        }

    @app.get("/internal/event-failures")
    def event_failures():
        return {
            "events": app.state.ctx.db.sql(
                "SELECT id,topic,error,created FROM dead_letters ORDER BY created LIMIT 100"
            )
        }

    @app.post("/internal/event-failures/{event_id}/retry")
    def retry_event(event_id: str, request: Request):
        who = actor(request, "admin")
        db = app.state.ctx.db
        with db.tx():
            row = db.sql("SELECT * FROM dead_letters WHERE id=%s", (event_id,), one=True)
            if not row:
                fail(404, "Failed event not found")
            if not handler or not row["topic"] or row["payload"].get("invalid"):
                fail(409, "Malformed events cannot be replayed; inspect the originating producer.")
            if not db.sql("SELECT id FROM inbox WHERE id=%s", (event_id,), one=True):
                try:
                    handler(app.state.ctx, row["topic"], row["payload"])
                except Exception:
                    fail(
                        409,
                        "The event still fails processing; correct its originating service first.",
                    )
                db.sql("INSERT INTO inbox VALUES(%s,%s)", (event_id, time.time()))
            db.sql("DELETE FROM dead_letters WHERE id=%s", (event_id,))
            db.audit(who["email"], "event.replayed", event_id)
        return {"status": "processed", "id": event_id}

    @app.post("/internal/pause")
    def pause(payload: dict):
        ctx = app.state.ctx
        seconds = min(300, max(0, int(payload.get("seconds", 0))))
        with ctx.activity:
            ctx.paused_until = time.time() + seconds
            if seconds and not ctx.activity.wait_for(lambda: ctx.active_steps == 0, timeout=20):
                fail(503, "Background operations have not yet quiesced; retry later.")
        return {"paused": bool(seconds)}

    @app.post("/internal/verify-snapshot")
    def verify_snapshot(payload: dict):
        if payload.get("service") != name:
            fail(422, "Wrong service snapshot")
        with app.state.ctx.db.tx() as con:
            con.execute(
                "CREATE TEMP TABLE restore_probe(collection TEXT NOT NULL,id TEXT NOT NULL,body JSONB NOT NULL,PRIMARY KEY(collection,id)) ON COMMIT DROP"
            )
            for row in payload["records"]:
                con.execute(
                    "INSERT INTO restore_probe VALUES(%s,%s,%s)",
                    (row["collection"], row["id"], Jsonb(row["body"])),
                )
            count = con.execute("SELECT count(*) AS n FROM restore_probe").fetchone()["n"]
        return {"service": name, "validated_records": count}

    @app.post("/internal/restore")
    def restore(payload: dict):
        if payload.get("service") != name:
            fail(422, "Wrong service snapshot")
        db = app.state.ctx.db
        with db.tx():
            db.sql("DELETE FROM documents WHERE collection NOT IN ('backups')")
            db.sql("DELETE FROM outbox WHERE sent IS NULL")
            for row in payload["records"]:
                if row["collection"] not in {"sessions", "attempts", "backups"}:
                    db.put(row["collection"], row["id"], row["body"])
            db.put("_system", "event_floor", {"time": payload.get("event_floor", time.time())})
        if hasattr(app.state, "after_restore"):
            app.state.after_restore(app.state.ctx)
        return {"restored": name, "records": len(payload["records"])}

    install(app)
    return app
