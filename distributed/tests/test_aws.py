"""Offline AWS integration contract tests; no AWS account or network is required."""

import base64
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helio.common import kafka_config, service_url
from helio.runtime import ECSRuntime
from helio.services.storage import Objects


class FakeECS:
    def __init__(self):
        self.desired = 1
        self.updated = []

    def describe_services(self, **kwargs):
        return {
            "failures": [],
            "services": [
                {
                    "desiredCount": self.desired,
                    "runningCount": self.desired,
                    "pendingCount": 0,
                    "deployments": [
                        {
                            "id": "ecs-svc/test",
                            "status": "PRIMARY",
                            "rolloutState": "COMPLETED",
                        }
                    ],
                }
            ],
        }

    def update_service(self, **kwargs):
        self.desired = kwargs["desiredCount"]
        self.updated.append(kwargs)


class FakeS3:
    def head_bucket(self, **kwargs):
        return {}


class AWSContracts(unittest.TestCase):
    def test_ecs_runtime_scales_and_verifies_convergence(self):
        runtime = ECSRuntime.__new__(ECSRuntime)
        runtime.cluster = "helio"
        runtime.service = "helio-workload"
        runtime.timeout = 1
        runtime.client = FakeECS()
        result = runtime.scale(3)
        self.assertTrue(result["converged"])
        self.assertEqual(result["observed"], 3)
        self.assertEqual(runtime.client.updated[0]["cluster"], "helio")

    def test_service_discovery_template(self):
        with patch.dict(
            os.environ,
            {"SERVICE_URL_TEMPLATE": "http://{service}.helio.internal:8000"},
            clear=False,
        ):
            self.assertEqual(service_url("auth"), "http://auth.helio.internal:8000")

    def test_local_kafka_configuration_remains_supported(self):
        with patch.dict(os.environ, {"KAFKA_BOOTSTRAP": "kafka:9092"}, clear=True):
            self.assertEqual(
                kafka_config(),
                {"bootstrap.servers": "kafka:9092", "security.protocol": "PLAINTEXT"},
            )

    def test_msk_iam_configuration_registers_oauth_callback(self):
        environment = {
            "KAFKA_BOOTSTRAP": "example.kafka.amazonaws.com:9098",
            "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
            "KAFKA_SASL_MECHANISM": "OAUTHBEARER",
            "AWS_REGION": "ap-south-1",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = kafka_config()
        self.assertEqual(config["security.protocol"], "SASL_SSL")
        self.assertEqual(config["sasl.mechanism"], "OAUTHBEARER")
        self.assertTrue(callable(config["oauth_cb"]))

    def test_native_s3_uses_task_credentials_without_static_keys(self):
        captured = {}

        def client(service, **kwargs):
            captured.update(kwargs)
            return FakeS3()

        environment = {
            "S3_BUCKET": "production-objects",
            "AWS_REGION": "ap-south-1",
            "STORAGE_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode(),
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("helio.services.storage.boto3.client", client),
        ):
            objects = Objects()
        self.assertEqual(objects.bucket, "production-objects")
        self.assertNotIn("aws_access_key_id", captured)
        self.assertNotIn("aws_secret_access_key", captured)
        self.assertNotIn("endpoint_url", captured)


if __name__ == "__main__":
    unittest.main()
