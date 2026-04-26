from __future__ import annotations

import subprocess

import pytest

from cli import _minio


def test_resolve_minio_endpoint_prefers_environment(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://env-minio:9000")

    assert _minio._resolve_minio_endpoint({}) == "http://env-minio:9000"


def test_resolve_minio_endpoint_prefers_env_file_over_docker(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)

    def fail_docker(*args, **kwargs):
        raise AssertionError("docker inspect should not be called when .env pins endpoint")

    monkeypatch.setattr(subprocess, "check_output", fail_docker)

    assert (
        _minio._resolve_minio_endpoint({"MINIO_ENDPOINT": "http://file-minio:9000"})
        == "http://file-minio:9000"
    )


def test_resolve_minio_endpoint_uses_live_docker_ip(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "192.168.97.11\n")

    assert _minio._resolve_minio_endpoint({}) == "http://192.168.97.11:9000"


def test_resolve_minio_endpoint_fails_fast_without_docker(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)

    def docker_unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="daemon unavailable")

    monkeypatch.setattr(subprocess, "check_output", docker_unavailable)

    with pytest.raises(RuntimeError, match="MinIO endpoint unavailable"):
        _minio._resolve_minio_endpoint({})
