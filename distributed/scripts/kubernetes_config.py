"""Generate a Compose configuration for an existing Kubernetes workload."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--workload-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--namespace", default="helio-workload")
    parser.add_argument("--deployment", default="checkout-api")
    args = parser.parse_args()
    if urlparse(args.api_url).scheme != "https":
        parser.error("Use the HTTPS Kubernetes API endpoint reachable from a Docker container.")
    if urlparse(args.workload_url).scheme not in {"http", "https"}:
        parser.error("Provide a reachable HTTP(S) workload endpoint.")
    for file in (args.token_file, args.ca_file):
        if not file.is_file():
            parser.error("Token and CA files must exist; their contents are never printed.")
    spec = json.loads((ROOT / "compose.yaml").read_text())
    service = spec["services"]["capacity"]
    service["user"] = "65532:65532"
    service["networks"] = ["backend"]
    service["volumes"] = [
        {
            "type": "bind",
            "source": str(args.token_file.resolve()),
            "target": "/run/secrets/kube-token",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(args.ca_file.resolve()),
            "target": "/run/secrets/kube-ca.crt",
            "read_only": True,
        },
    ]
    service["environment"].update(
        RUNTIME="kubernetes",
        KUBE_API_URL=args.api_url,
        KUBE_WORKLOAD_URL=args.workload_url,
        KUBE_NAMESPACE=args.namespace,
        KUBE_DEPLOYMENT=args.deployment,
        KUBE_TOKEN_FILE="/run/secrets/kube-token",
        KUBE_CA_FILE="/run/secrets/kube-ca.crt",
    )
    output = ROOT / "compose.kubernetes.generated.yaml"
    output.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(
        "Created distributed/compose.kubernetes.generated.yaml. Docker socket access is removed from this controller."
    )
    print(
        "Run: docker compose --env-file distributed/.env -f distributed/compose.kubernetes.generated.yaml up -d --no-build"
    )
    print(
        "This configures the adapter; it does not certify that your cluster is reachable or healthy."
    )


if __name__ == "__main__":
    main()
