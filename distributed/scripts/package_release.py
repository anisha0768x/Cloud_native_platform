"""Build a source-only release from an explicit allowlist, with content hashes."""

import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output" / "Source" / "Helio_Cloud_Operations_Verified_2026-10-01.zip"
PREFIX = "Helio_Cloud_Operations/"
ROOT_FILES = [
    "README.md",
    "README_VSCODE.md",
    "ARCHITECTURE_ALIGNMENT.md",
    "run_local.py",
    "launch_platform.py",
    "app.py",
    "Start-Cloud-Native-Platform.cmd",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    "Dockerfile",
    "docker-compose.yml",
]
DOCS = ["ARCHITECTURE_V3.md", "CLOUD_STATUS.md", "KUBERNETES_ADAPTER.md", "VERIFICATION.md"]


def allowed(path):
    return (
        not any(
            part.startswith(".env") or part in {"__pycache__", ".pytest_cache", ".secrets"}
            for part in path.parts
        )
        and path.suffix not in {".pyc", ".key", ".pem"}
        and ".generated." not in path.name
    )


def main():
    files = [ROOT / name for name in ROOT_FILES]
    files += [ROOT / "docs" / name for name in DOCS]
    for folder in ("web", "distributed", ".vscode", ".github"):
        files.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file() and allowed(path.relative_to(ROOT))
        )
    files += [ROOT / "services/workload/worker.py", ROOT / "services/workload/Dockerfile"]
    files += list((ROOT / "docs/verification").glob("*.json"))
    files = sorted(set(files))
    if any(not p.is_file() for p in files):
        raise SystemExit("Release documentation or source is missing.")
    secrets = []
    for env in (ROOT / "distributed").glob(".env*"):
        for line in env.read_text().splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if len(value) > 16 and any(
                word in key for word in ("PASSWORD", "SECRET", "ENCRYPTION_KEY", "SETUP_TOKEN")
            ):
                secrets.append(value.encode())
    entries = {}
    for file in files:
        data = file.read_bytes()
        if any(secret in data for secret in secrets):
            raise SystemExit(
                "Refusing to package a generated secret in " + str(file.relative_to(ROOT))
            )
        entries[file.relative_to(ROOT).as_posix()] = data
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    entries["SOURCE_MANIFEST.json"] = (
        json.dumps({"version": "3.0.0", "algorithm": "SHA-256", "files": manifest}, indent=2) + "\n"
    ).encode()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries.items():
            archive.writestr(PREFIX + name, data)
    with zipfile.ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read(PREFIX + name)).hexdigest() == digest
    checksum = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix(".zip.sha256").write_text(
        checksum + "  " + OUTPUT.name + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "zip": str(OUTPUT),
                "source_files": len(manifest),
                "bytes": OUTPUT.stat().st_size,
                "sha256": checksum,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
