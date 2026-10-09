from email.message import EmailMessage
import os
import smtplib
import ssl
import time
from urllib.parse import urlparse
import httpx
import boto3
from fastapi import Request
from helio.common import actor, fail, rpc, service_app, stamp, uid
from helio.contracts import NotificationRequest


def channels():
    return [
        {"name": "inbox", "configured": True, "description": "Persistent workspace inbox"},
        {
            "name": "email",
            "configured": bool(os.environ.get("SMTP_HOST") or os.environ.get("SES_FROM")),
            "description": "Amazon SES in AWS; SMTP capture in Mailpit locally",
        },
        {
            "name": "slack",
            "configured": bool(os.environ.get("SLACK_WEBHOOK")),
            "description": "Configured Slack receiver; disabled until supplied",
        },
        {
            "name": "webhook",
            "configured": bool(os.environ.get("NOTIFICATION_WEBHOOK")),
            "description": "Configured HTTPS receiver",
        },
    ]


def enqueue(db, incident, channel):
    if not any(c["name"] == channel and c["configured"] for c in channels()):
        fail(409, "This channel is not configured.")
    key = incident["id"] + ":" + channel
    prior = db.get("notices", key)
    if prior:
        return prior
    row = {
        "id": key,
        "incident_id": incident["id"],
        "title": incident["title"],
        "service": incident.get("service", incident["service_id"]),
        "channel": channel,
        "status": "delivered" if channel == "inbox" else "queued",
        "attempts": 0,
        "error": None,
        "created_at": stamp(),
        "delivered_at": stamp() if channel == "inbox" else None,
        "read_at": None,
        "next_attempt": time.time(),
    }
    db.put("notices", key, row)
    return row


def consume(ctx, topic, event):
    enqueue(ctx.db, event["data"], "inbox")


def tick(ctx):
    due = ctx.db.sql(
        "SELECT body FROM documents WHERE collection='notices' AND body->>'status'='queued' AND (body->>'next_attempt')::double precision<=%s ORDER BY updated LIMIT 5",
        (time.time(),),
    )
    for record in due:
        row = record["body"]
        row["attempts"] += 1
        message = f"Helio incident: {row['title']} — {row['service']} — {row['incident_id']}"
        try:
            if row["channel"] == "email":
                sender = os.environ.get("SES_FROM", os.environ.get("SMTP_FROM", "helio@localhost"))
                recipient = os.environ.get(
                    "SES_TO", os.environ.get("SMTP_TO", "operator@localhost")
                )
                if os.environ.get("EMAIL_PROVIDER") == "ses":
                    boto3.client("sesv2", region_name=os.environ.get("AWS_REGION")).send_email(
                        FromEmailAddress=sender,
                        Destination={"ToAddresses": [recipient]},
                        Content={
                            "Simple": {
                                "Subject": {"Data": "Helio incident notification"},
                                "Body": {"Text": {"Data": message}},
                            }
                        },
                    )
                    row["status"] = "accepted"
                    row.update(delivered_at=stamp(), error=None)
                    ctx.db.put("notices", row["id"], row)
                    continue
                mail = EmailMessage()
                mail["Subject"] = "Helio incident notification"
                mail["From"] = sender
                mail["To"] = recipient
                mail["Message-ID"] = "<" + row["id"].replace(":", ".") + "@helio.local>"
                mail.set_content(message)
                host = os.environ["SMTP_HOST"]
                with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=5) as smtp:
                    if host not in {"mailpit", "localhost", "127.0.0.1"}:
                        smtp.starttls(context=ssl.create_default_context())
                    if os.environ.get("SMTP_USER"):
                        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    smtp.send_message(mail)
                row["status"] = "accepted"
            else:
                url = os.environ[
                    "SLACK_WEBHOOK" if row["channel"] == "slack" else "NOTIFICATION_WEBHOOK"
                ]
                parsed = urlparse(url)
                if parsed.scheme != "https" and parsed.hostname not in {
                    "localhost",
                    "127.0.0.1",
                    "webhook-receiver",
                }:
                    raise ValueError("HTTPS required")
                payload = (
                    {"text": message}
                    if row["channel"] == "slack"
                    else {"id": row["id"], "incident_id": row["incident_id"], "message": message}
                )
                with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                    client.post(
                        url, json=payload, headers={"Idempotency-Key": row["id"]}
                    ).raise_for_status()
                row["status"] = "delivered"
            row.update(delivered_at=stamp(), error=None)
        except Exception:
            row.update(
                status="failed" if row["attempts"] >= 3 else "queued",
                error="Receiver did not confirm delivery. Check connection settings.",
                next_attempt=time.time() + 2 ** row["attempts"],
            )
        ctx.db.put("notices", row["id"], row)


def install(app):
    def db():
        return app.state.ctx.db

    @app.get("/api/v1/notifications")
    def notices(request: Request):
        actor(request)
        return {"notifications": db().rows("notices", 100), "channels": channels()}

    @app.post("/api/v1/notifications/send")
    def send(payload: NotificationRequest, request: Request):
        who = actor(request, "operate")
        incident = rpc("catalog", "/internal/incidents/" + payload.incident_id)
        with db().tx():
            row = enqueue(db(), incident, payload.channel)
            db().audit(who["email"], "notification.requested", row["id"])
        return row

    @app.post("/api/notifications/{notice_id}/read")
    def read(notice_id: str, request: Request):
        actor(request, "operate")
        row = db().get("notices", notice_id)
        if not row:
            fail(404, "Notification not found")
        row["read_at"] = stamp()
        db().put("notices", notice_id, row)
        return {"ok": True}

    @app.post("/api/notifications/{notice_id}/retry")
    def retry(notice_id: str, request: Request):
        who = actor(request, "operate")
        row = db().get("notices", notice_id)
        if not row or row["status"] != "failed":
            fail(409, "Only failed notifications can be retried.")
        row.update(status="queued", attempts=0, next_attempt=time.time(), error=None)
        db().put("notices", notice_id, row)
        db().audit(who["email"], "notification.retried", notice_id)
        return {"status": "queued"}


app = service_app("notifications", install, ["incidents.opened"], consume, tick)
