"""Shared MinIO client factory. Used by backup_chroma, backup_neo4j,
backup_verify, and restore_chroma — don't fork this."""
from __future__ import annotations

import os
from pathlib import Path


def s3_client():
    """Return a boto3 S3 client configured for the local MinIO instance.
    Credentials are read from ~/server/minio/.env (MINIO_ROOT_USER /
    MINIO_ROOT_PASSWORD). The endpoint defaults to 192.168.97.5:9000 but
    can be overridden via the MINIO_ENDPOINT env var.
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
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://192.168.97.5:9000"),
        aws_access_key_id=creds.get("MINIO_ROOT_USER", ""),
        aws_secret_access_key=creds.get("MINIO_ROOT_PASSWORD", ""),
        config=Config(signature_version="s3v4"),
    )
