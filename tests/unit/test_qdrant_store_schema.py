from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from qdrant_store import QdrantStore  # noqa: E402


class _Collection:
    def __init__(self, name: str):
        self.name = name


class _Collections:
    def __init__(self, names: list[str]):
        self.collections = [_Collection(name) for name in names]


class _FakeClient:
    def __init__(self, names: list[str]):
        self.names = set(names)
        self.deleted = []
        self.created = []

    def get_collections(self):
        return _Collections(sorted(self.names))

    def get_collection(self, collection_name: str):
        class Params:
            vectors = {}

        class Config:
            params = Params()

        class Info:
            config = Config()

        return Info()

    def delete_collection(self, collection_name: str):
        self.deleted.append(collection_name)
        self.names.discard(collection_name)

    def create_collection(self, collection_name: str, vectors_config):
        self.created.append((collection_name, vectors_config))
        self.names.add(collection_name)


def test_healthcheck_probe_recreated_when_dense_slot_missing():
    store = QdrantStore.__new__(QdrantStore)
    store._client = _FakeClient(["healthcheck_probe"])

    store.create_collection("healthcheck_probe")

    assert store._client.deleted == ["healthcheck_probe"]
    assert store._client.created[0][0] == "healthcheck_probe"
    assert "dense" in store._client.created[0][1]


def test_existing_non_probe_collection_is_not_recreated_when_present():
    store = QdrantStore.__new__(QdrantStore)
    store._client = _FakeClient(["knowledge"])

    store.create_collection("knowledge")

    assert store._client.deleted == []
    assert store._client.created == []
