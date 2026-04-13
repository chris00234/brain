"""Shared MinIO client factory. Used by backup_chroma, backup_neo4j,
backup_verify, and restore_chroma — don't fork this."""
from __future__ import annotations

import os
from pathlib import Path


def s3_client():
    """Return a boto3 S3 client configured for the local MinIO instance.
    Credentials are read from ~/server/minio/.env (MINIO_ROOT_USER /
    MINIO_ROOT_PASSWORD). Endpoint resolution order:
      1. MINIO_ENDPOINT env var (pinned override)
      2. MINIO_ENDPOINT from ~/server/minio/.env
      3. `docker inspect minio` NetworkSettings (runtime discovery)
      4. http://192.168.97.8:9000 (known-good fallback 2026-04-12)
    """
    import boto3
    from botocore.config import Config
    env_path = Path("/Users/chrischo/server/minio/.env")
    creds: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                creds[k] = v
    return boto3.client(
        "s3",
        endpoint_url=_resolve_minio_endpoint(creds),
        aws_access_key_id=creds.get("MINIO_ROOT_USER", ""),
        aws_secret_access_key=creds.get("MINIO_ROOT_PASSWORD", ""),
        config=Config(signature_version="s3v4"),
    )


def _resolve_minio_endpoint(creds: dict[str, str]) -> str:
    env = os.getenv("MINIO_ENDPOINT")
    if env:
        return env
    if creds.get("MINIO_ENDPOINT"):
        return creds["MINIO_ENDPOINT"]
    try:
        import subprocess
        out = subprocess.check_output(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "minio"],
            text=True, timeout=3,
        ).strip()
        if out:
            return f"http://{out}:9000"
    except Exception:
        pass
    return "http://192.168.97.8:9000"
