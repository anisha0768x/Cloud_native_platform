import base64
import hashlib
import io
import json
import os
from pathlib import PurePath
import secrets
import time
from urllib.parse import quote
import zipfile
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request, Response, UploadFile, File, Form
from helio.common import NAMES, actor, fail, rpc, service_app, stamp, uid

MAX_FILE = 10 * 1024 * 1024


class Objects:
    def __init__(self):
        self.bucket = os.environ.get("S3_BUCKET", "helio-objects")
        endpoint = os.environ.get("S3_ENDPOINT")
        options = {
            "region_name": os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "us-east-1")),
            "config": Config(
                connect_timeout=3,
                read_timeout=15,
                retries={"max_attempts": 2},
                s3={
                    "addressing_style": os.environ.get(
                        "S3_ADDRESSING_STYLE", "path" if endpoint else "auto"
                    )
                },
            ),
        }
        if endpoint:
            options["endpoint_url"] = endpoint
        if os.environ.get("S3_ACCESS_KEY"):
            options["aws_access_key_id"] = os.environ["S3_ACCESS_KEY"]
            options["aws_secret_access_key"] = os.environ["S3_SECRET_KEY"]
        self.s3 = boto3.client("s3", **options)
        self.cipher = AESGCM(base64.urlsafe_b64decode(os.environ["STORAGE_ENCRYPTION_KEY"]))
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            auto_create = os.environ.get("S3_AUTO_CREATE", "true" if endpoint else "false")
            if auto_create.lower() != "true":
                raise RuntimeError("Configured S3 bucket is unavailable") from exc
            self.s3.create_bucket(Bucket=self.bucket)
        if endpoint:
            self.s3.put_bucket_versioning(
                Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"}
            )

    def write(self, key, data):
        nonce = secrets.token_bytes(12)
        blob = nonce + self.cipher.encrypt(nonce, data, key.encode())
        result = self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=blob, ContentType="application/octet-stream"
        )
        return result.get("VersionId"), len(blob)

    def read(self, key, version=None):
        extra = {"VersionId": version} if version else {}
        blob = self.s3.get_object(Bucket=self.bucket, Key=key, **extra)["Body"].read()
        return self.cipher.decrypt(blob[:12], blob[12:], key.encode())


def install(app):
    def initialize(ctx):
        ctx.objects = Objects()

    app.state.initialize = initialize
    app.state.check_dependency = lambda ctx: ctx.objects.s3.head_bucket(Bucket=ctx.objects.bucket)

    def ctx():
        return app.state.ctx

    def db():
        return ctx().db

    def inventory():
        return db().rows("objects", 10000)

    @app.get("/api/cloud/storage")
    def files(request: Request):
        actor(request)
        return {
            "objects": inventory(),
            "backend": (
                "S3-compatible object service (SeaweedFS locally)"
                if os.environ.get("S3_ENDPOINT") == "http://objects:8333"
                else "Configured S3-compatible object service"
            ),
            "encryption": "AES-256-GCM",
            "max_bytes": MAX_FILE,
            "database_encrypted": False,
        }

    @app.get("/internal/usage")
    def usage():
        return {"bytes": sum(o["size"] for o in inventory()), "objects": len(inventory())}

    @app.post("/api/cloud/storage", status_code=201)
    async def upload(
        request: Request, file: UploadFile = File(...), retention_days: int = Form(0, ge=0, le=3650)
    ):
        who = actor(request, "storage")
        data = await file.read(MAX_FILE + 1)
        await file.close()
        if not data or len(data) > MAX_FILE:
            fail(422, "Upload a nonempty file of at most 10 MB.")
        name = (file.filename or "upload.bin").replace("\\", "/").split("/")[-1][:200]
        with db().tx():
            version = (
                max([o["version"] for o in db().rows("objects", 10000, {"name": name})] + [0]) + 1
            )
            key = uid("object")
            s3version, _ = ctx().objects.write("objects/" + key, data)
            row = {
                "id": key,
                "name": name,
                "version": version,
                "size": len(data),
                "checksum": hashlib.sha256(data).hexdigest(),
                "content_type": file.content_type,
                "retention_until": time.time() + retention_days * 86400,
                "actor": who["email"],
                "created_at": stamp(),
                "s3_version": s3version,
            }
            db().put("objects", key, row)
            db().audit(
                who["email"], "object.uploaded", key, {"size": len(data), "version": version}
            )
        return row

    @app.get("/api/cloud/storage/{object_id}/download")
    def download(object_id: str, request: Request):
        who = actor(request)
        row = db().get("objects", object_id)
        if not row:
            fail(404, "Object not found")
        try:
            data = ctx().objects.read("objects/" + object_id, row["s3_version"])
            if hashlib.sha256(data).hexdigest() != row["checksum"]:
                raise ValueError()
        except Exception:
            fail(409, "Object integrity verification failed.")
        db().audit(who["email"], "object.downloaded", object_id)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["name"]),
                "X-Content-SHA256": row["checksum"],
            },
        )

    @app.delete("/api/cloud/storage/{object_id}")
    def delete(object_id: str, request: Request):
        who = actor(request, "storage")
        with db().tx():
            row = db().get("objects", object_id)
            if not row:
                fail(404, "Object not found")
            if time.time() < row["retention_until"]:
                fail(409, "This version is still protected by its application retention policy.")
            options = {"VersionId": row["s3_version"]} if row["s3_version"] else {}
            ctx().objects.s3.delete_object(
                Bucket=ctx().objects.bucket, Key="objects/" + object_id, **options
            )
            db().delete("objects", object_id)
            db().audit(who["email"], "object.deleted", object_id)
        return {"ok": True}

    @app.post("/internal/archive")
    def archive(payload: dict):
        key = uid("backup")
        buffer = io.BytesIO()
        manifest = {
            "schema": 3,
            "snapshots": payload["snapshots"],
            "created_at": stamp(),
            "objects": inventory(),
        }
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip:
            zip.writestr("manifest.json", json.dumps(manifest))
            for row in manifest["objects"]:
                data = ctx().objects.read("objects/" + row["id"], row["s3_version"])
                if hashlib.sha256(data).hexdigest() != row["checksum"]:
                    fail(409, "Source object failed checksum verification.")
                zip.writestr("objects/" + row["id"], data)
        version, size = ctx().objects.write("backups/" + key, buffer.getvalue())
        return {
            "id": key,
            "created_at": stamp(),
            "checksum": hashlib.sha256(buffer.getvalue()).hexdigest(),
            "s3_version": version,
            "size": size,
            "verified_at": None,
            "verify_seconds": None,
        }

    def read_archive(payload):
        started = time.perf_counter()
        data = ctx().objects.read("backups/" + payload["id"], payload["s3_version"])
        if hashlib.sha256(data).hexdigest() != payload["checksum"]:
            fail(409, "Backup checksum mismatch.")
        with zipfile.ZipFile(io.BytesIO(data)) as zip:
            manifest = json.loads(zip.read("manifest.json"))
            if manifest["schema"] != 3:
                fail(409, "Unsupported archive schema.")
            if sorted(s["service"] for s in manifest["snapshots"]) != sorted(NAMES):
                fail(409, "Archive must contain exactly one snapshot per service.")
            for row in manifest["objects"]:
                content = zip.read("objects/" + row["id"])
                if hashlib.sha256(content).hexdigest() != row["checksum"]:
                    fail(409, "Backup object checksum mismatch.")
        return data, manifest, round(time.perf_counter() - started, 4)

    @app.post("/internal/archive/verify")
    def verify(payload: dict):
        data, manifest, seconds = read_archive(payload)
        began = time.perf_counter()
        counts = {
            snapshot["service"]: rpc(
                snapshot["service"], "/internal/verify-snapshot", "POST", snapshot, timeout=30
            )["validated_records"]
            for snapshot in manifest["snapshots"]
        }
        return {
            "status": "verified",
            "seconds": round(seconds + time.perf_counter() - began, 4),
            "restored_rows": counts | {"objects": len(manifest["objects"])},
            "method": "Decrypt archive, verify every object checksum, and insert each service snapshot into an isolated temporary PostgreSQL table.",
        }

    @app.post("/internal/archive/read")
    def read(payload: dict):
        data, manifest, seconds = read_archive(payload)
        return manifest

    @app.post("/internal/archive/restore-objects")
    def restore_objects(payload: dict):
        data, manifest, seconds = read_archive(payload)
        with zipfile.ZipFile(io.BytesIO(data)) as zip:
            for row in manifest["objects"]:
                version, _ = ctx().objects.write(
                    "objects/" + row["id"], zip.read("objects/" + row["id"])
                )
                row["s3_version"] = version
                db().put("objects", row["id"], row)
        return {"restored_objects": len(manifest["objects"])}


app = service_app("storage", install)
