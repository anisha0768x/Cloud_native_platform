"""Launcher regressions: run with python -m unittest discover -s distributed/tests -p test_startup.py."""

import gzip
from http.client import RemoteDisconnected
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from distributed.scripts import start, local_database as db


class StartupTests(unittest.TestCase):
    def gateway(self, running=True, restarting=False):
        return {
            "Config": {"Labels": {"com.docker.compose.service": "gateway"}},
            "HostConfig": {
                "PortBindings": {"8000/tcp": [{"HostPort": "8080", "HostIp": "127.0.0.1"}]}
            },
            "State": {
                "Running": running,
                "Restarting": restarting,
                "Health": {"Status": "unhealthy"},
            },
        }

    def test_unhealthy_gateway_can_be_recovered(self):
        self.assertTrue(start.gateway_owns_port([self.gateway()], 8080))
        self.assertTrue(start.gateway_owns_port([self.gateway(False, True)], 8080))

    def test_stopped_or_other_port_is_not_treated_as_owned(self):
        self.assertFalse(start.gateway_owns_port([self.gateway(False)], 8080))
        self.assertFalse(start.gateway_owns_port([self.gateway()], 8081))

    def test_other_installation_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            container = self.gateway()
            container["Config"]["Labels"]["com.docker.compose.project.working_dir"] = str(
                Path(directory) / "other"
            )
            with self.assertRaisesRegex(SystemExit, "different folder"):
                start.verify_project_location([container], Path(directory) / "requested")

    def test_old_dependency_label_does_not_block_owned_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = self.gateway()
            gateway["Config"]["Labels"]["com.docker.compose.project.working_dir"] = str(root)
            kafka = {
                "Config": {
                    "Labels": {
                        "com.docker.compose.service": "kafka",
                        "com.docker.compose.project.working_dir": str(root / "old"),
                    }
                }
            }
            start.verify_project_location([kafka, gateway], root)

    def test_stopped_project_can_be_moved(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = self.gateway(False)
            gateway["Config"]["Labels"]["com.docker.compose.project.working_dir"] = str(
                Path(directory) / "old"
            )
            start.verify_project_location([gateway], Path(directory) / "new")

    def test_disconnected_http_reports_error_without_traceback(self):
        output = io.StringIO()
        with (
            patch.object(start, "main", side_effect=RemoteDisconnected()),
            patch("sys.stderr", output),
        ):
            with self.assertRaises(SystemExit) as error:
                start.entrypoint()
        self.assertEqual(error.exception.code, 1)
        self.assertIn("Data volumes were preserved", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_compose_error_handled_by_supported_entrypoint(self):
        with (
            patch.object(start, "main", side_effect=subprocess.CalledProcessError(1, ["docker"])),
            patch("sys.stderr", io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit):
                start.entrypoint()
        self.assertIn("logs --tail", output.getvalue())

    def values(self):
        return {
            "POSTGRES_PASSWORD": "private-database-secret",
            "COMPOSE_PROJECT_NAME": "helio-test",
            "INTERNAL_SECRET": "internal",
            "SETUP_TOKEN": "setup",
            "S3_SECRET_KEY": "s3",
            "AUTH_ENCRYPTION_KEY": "preserve-auth",
            "STORAGE_ENCRYPTION_KEY": "preserve-storage",
            **{name.upper() + "_DB_PASSWORD": "private-service-secret" for name in db.SERVICES},
        }

    def test_all_database_passwords_change_in_one_transaction(self):
        values = self.values()
        values["AUTH_DB_PASSWORD"] = "quote'and\\slash"
        sql = db.password_sql(values)
        self.assertEqual(sql.count("ALTER ROLE"), 13)
        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertTrue(sql.endswith("COMMIT;"))
        self.assertIn("quote''and\\slash", sql)
        self.assertNotIn("DROP", sql)

    def test_missing_password_cannot_apply_partial_repair(self):
        values = self.values()
        values["DASHBOARD_DB_PASSWORD"] = ""
        with self.assertRaisesRegex(SystemExit, "DASHBOARD_DB_PASSWORD"):
            db.password_sql(values)

    def test_login_failure_is_actionable_without_secret_output(self):
        result = subprocess.CompletedProcess(
            [], 1, b"notifications\ndashboard\n", b"secret-sensitive-diagnostic"
        )
        with patch.object(db.subprocess, "run", return_value=result) as run:
            with self.assertRaises(SystemExit) as error:
                db.check_database(["docker", "compose"], {})
        self.assertIsInstance(run.call_args.kwargs["input"], bytes)
        self.assertNotIn(b"\r", run.call_args.kwargs["input"])
        message = str(error.exception)
        self.assertIn("notifications, dashboard", message)
        self.assertIn("--repair-database", message)
        self.assertNotIn("secret-sensitive", message)

    def test_failed_backup_prevents_password_changes(self):
        self.run_recovery(fail_dump=True)

    def test_rotation_preserves_encryption_keys_and_saves_backup(self):
        self.run_recovery(fail_dump=False)

    def run_recovery(self, fail_dump):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if "pg_dumpall" in command:
                kwargs["stdout"].write(b"-- PostgreSQL database cluster dump complete\n")
                return subprocess.CompletedProcess(command, int(fail_dump))
            if any("SELECT rolname" in part for part in command):
                return subprocess.CompletedProcess(
                    command, 0, "\n".join("helio_" + n for n in db.SERVICES)
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("original-private-configuration")
            values = self.values()
            with (
                patch.object(db.subprocess, "run", side_effect=fake_run),
                patch("sys.stdout", io.StringIO()),
            ):
                if fail_dump:
                    with self.assertRaisesRegex(SystemExit, "backup failed"):
                        db.repair_database(["docker", "compose"], values, env, root, rotate=True)
                    self.assertFalse(any("input" in kw for _, kw in calls))
                    self.assertEqual(env.read_text(), "original-private-configuration")
                    return
                updated = db.repair_database(["docker", "compose"], values, env, root, rotate=True)
            for key in ("AUTH_ENCRYPTION_KEY", "STORAGE_ENCRYPTION_KEY"):
                self.assertEqual(updated[key], values[key])
            self.assertNotEqual(updated["AUTH_DB_PASSWORD"], values["AUTH_DB_PASSWORD"])
            backups = list((root / ".recovery").iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                (backups[0] / "environment-before.env").read_text(),
                "original-private-configuration",
            )
            with gzip.open(backups[0] / "postgres-before.sql.gz", "rb") as f:
                self.assertIn(b"dump complete", f.read())
            sql_calls = [(cmd, kw) for cmd, kw in calls if "input" in kw]
            self.assertEqual(len(sql_calls), 1)
            self.assertNotIn(updated["AUTH_DB_PASSWORD"], " ".join(sql_calls[0][0]))


if __name__ == "__main__":
    unittest.main()
