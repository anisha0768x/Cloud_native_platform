"""Build and start the distributed project without installing host runtime packages."""

import argparse
from http.client import HTTPException
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

from distributed.scripts.local_database import check_database, repair_database

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "distributed"


def inspect_project(docker, project):
    result = subprocess.run(
        [docker, "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    if not result.stdout.split():
        return []
    result = subprocess.run(
        [docker, "inspect", *result.stdout.split()],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def verify_project_location(containers, config):
    # Compose retains old labels on unchanged Kafka/S3 containers after a folder move.
    # The gateway and database anchor ownership of a live deployment.
    live = [
        c
        for c in containers
        if c.get("State", {}).get("Running") or c.get("State", {}).get("Restarting")
    ]
    anchors = [
        c
        for c in live
        if (c["Config"].get("Labels") or {}).get("com.docker.compose.service")
        in {"gateway", "postgres"}
    ]
    for container in anchors or live:
        labels = container["Config"].get("Labels") or {}
        location = labels.get("com.docker.compose.project.working_dir")
        if location and Path(location).resolve() != config.resolve():
            raise SystemExit(
                "This Compose project is already managed from a different folder: "
                + location
                + "\nUse that folder, or give this copy a distinct COMPOSE_PROJECT_NAME, "
                "HELIO_PORT and MAIL_PORT in distributed/.env. No containers were changed."
            )


def gateway_owns_port(containers, port):
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.service") != "gateway":
            continue
        bindings = container.get("HostConfig", {}).get("PortBindings", {}).get("8000/tcp") or []
        if any(
            b.get("HostPort") == str(port) and b.get("HostIp") in ("", "0.0.0.0", "127.0.0.1")
            for b in bindings
        ):
            state = container.get("State", {})
            return bool(state.get("Running") or state.get("Restarting"))
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="Use existing application images")
    parser.add_argument("--open", action="store_true", help="Open the website after startup")
    parser.add_argument(
        "--stop", action="store_true", help="Stop this project; preserve all data volumes"
    )
    parser.add_argument(
        "--no-token", action="store_true", help="Do not print the initial setup token"
    )
    parser.add_argument(
        "--repair-database",
        action="store_true",
        help="Back up existing databases and align their login passwords with .env",
    )
    parser.add_argument(
        "--rotate-access-secrets",
        action="store_true",
        help="With --repair-database, replace access credentials; preserve encryption keys",
    )
    args = parser.parse_args(argv)
    if args.rotate_access_secrets and not args.repair_database:
        parser.error("--rotate-access-secrets requires --repair-database")
    if args.stop and args.repair_database:
        parser.error("--stop cannot be combined with --repair-database")
    docker = shutil.which("docker")
    if not docker and os.name == "nt":
        candidate = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Docker/Docker/resources/bin/docker.exe"
        )
        if candidate.exists():
            docker = str(candidate)
    if not docker:
        raise SystemExit(
            "Docker CLI not found. Install Docker Desktop, enable Linux containers, and open it."
        )
    check = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=30
    )
    if check.returncode:
        raise SystemExit(
            "Docker engine is unavailable. Open Docker Desktop, wait for Engine running, and retry."
        )

    spec = importlib.util.spec_from_file_location("helio_setup", CONFIG / "scripts/setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    env_file = setup.configure()
    setup.compose()
    values = dict(
        line.split("=", 1)
        for line in env_file.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    compose = [docker, "compose", "--env-file", str(env_file), "-f", str(CONFIG / "compose.yaml")]
    child_env = dict(os.environ, **values)
    containers = inspect_project(docker, values["COMPOSE_PROJECT_NAME"])
    verify_project_location(containers, CONFIG)

    def run(command):
        subprocess.run(command, cwd=ROOT, check=True, env=child_env)

    if args.stop:
        run(compose + ["stop"])
        scope = values["COMPOSE_PROJECT_NAME"] + "-workloads"
        result = subprocess.run(
            [docker, "ps", "-aq", "--filter", "label=io.helio.workspace=" + scope],
            capture_output=True,
            text=True,
            check=True,
        )
        for container in result.stdout.split():
            inspect = subprocess.run(
                [docker, "inspect", container], capture_output=True, text=True, check=True
            )
            if (
                json.loads(inspect.stdout)[0]["Config"].get("Labels", {}).get("io.helio.workspace")
                == scope
            ):
                run([docker, "rm", "-f", container])
        print("Project stopped. Database and object volumes were preserved.")
        return

    port = int(values.get("HELIO_PORT", "8080"))
    url = f"http://127.0.0.1:{port}"
    with socket.socket() as probe:
        occupied = probe.connect_ex(("127.0.0.1", port)) == 0
    if occupied and not gateway_owns_port(containers, port):
        raise SystemExit(
            f"Port {port} belongs to another application or Helio installation. "
            "Stop that application or change HELIO_PORT in distributed/.env. Nothing was stopped."
        )
    if occupied:
        print(
            "Existing project gateway found; checking its dependencies before restarting.",
            flush=True,
        )
    if not args.no_build:
        run([docker, "build", "-t", "helio-worker:v3", str(ROOT / "services/workload")])
        run(
            [
                docker,
                "build",
                "-t",
                "helio-platform:v3",
                "-f",
                str(CONFIG / "Dockerfile"),
                str(ROOT),
            ]
        )
    if args.repair_database:
        values = repair_database(compose, values, env_file, ROOT, args.rotate_access_secrets)
        child_env = dict(os.environ, **values)
    run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "postgres"])
    check_database(compose, child_env)
    run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "240"])
    with urlopen(url + "/api/auth/setup-status", timeout=10) as response:
        first_run = json.load(response)["required"]
    print("\nWebsite: " + url)
    print("Local SMTP inbox: http://127.0.0.1:" + values.get("MAIL_PORT", "8025"))
    if first_run and not args.no_token:
        print("First-administrator setup token (keep private): " + values["SETUP_TOKEN"])
        print("Paste this token into the setup form, then choose your own account and password.")
    print(
        "Containers keep running after this terminal closes. Stop with: python run_local.py --stop"
    )
    if args.open:
        webbrowser.open(url)


def entrypoint(argv=None):
    try:
        main(argv)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(
            "Startup did not finish; data volumes were preserved. Inspect service status with:\n"
            "  docker compose --env-file distributed/.env -f distributed/compose.yaml ps\n"
            "Inspect the failing service using the same command followed by logs --tail 60 SERVICE.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (URLError, HTTPException, TimeoutError, ConnectionError, json.JSONDecodeError):
        print(
            "The gateway did not return a healthy response. Re-run the launcher and inspect "
            "the gateway/auth service logs if it persists. Data volumes were preserved.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
