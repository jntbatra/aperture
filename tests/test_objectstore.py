"""Uploaded files go to object storage when it is configured."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sqlagent.api.app as api
from sqlagent.api.app import app
from sqlagent.config import Settings, settings
from sqlagent.objectstore import ObjectStore, ObjectStoreError, object_store


class FakeS3:
    def __init__(self, fail=False):
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def upload_file(self, path, bucket, key):
        if self.fail:
            raise RuntimeError("boom")
        self.objects[f"{bucket}/{key}"] = Path(path).read_bytes()

    def list_objects_v2(self, Bucket, Prefix):
        keys = [k.split("/", 1)[1] for k in self.objects if k.startswith(f"{Bucket}/{Prefix}")]
        return {"Contents": [{"Key": k} for k in keys]}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(f"{Bucket}/{item['Key']}", None)


def test_no_endpoint_means_no_store():
    assert object_store(Settings()) is None


def test_key_keeps_only_the_file_name():
    assert ObjectStore.key("abc", "../../etc/passwd") == "datasets/abc/passwd"


def test_put_and_delete_round_trip(tmp_path):
    fake = FakeS3()
    store = ObjectStore(Settings(), client=fake)
    source = tmp_path / "x.csv"
    source.write_text("a,b\n1,2\n")
    store.put("d1", "x.csv", source)
    assert fake.objects == {"assets/datasets/d1/x.csv": b"a,b\n1,2\n"}
    assert store.delete_dataset("d1") == 1
    assert fake.objects == {}


def test_failed_put_raises_a_plain_error(tmp_path):
    source = tmp_path / "x.csv"
    source.write_text("a\n1\n")
    with pytest.raises(ObjectStoreError):
        ObjectStore(Settings(), client=FakeS3(fail=True)).put("d1", "x.csv", source)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLAGENT_DATA_DIR", str(tmp_path))
    settings.cache_clear()
    api.get_store.cache_clear()
    with TestClient(app) as c:
        yield c
    api.get_store.cache_clear()
    settings.cache_clear()


def test_upload_is_stored_and_delete_removes_it(client, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(api, "object_store", lambda config: ObjectStore(config, client=fake))
    created = client.post("/api/datasets", files={"file": ("sales.csv", b"a,b\n1,2\n3,4\n")})
    assert created.status_code == 200, created.text
    dataset_id = created.json()["id"]
    assert list(fake.objects) == [f"assets/datasets/{dataset_id}/sales.csv"]

    assert client.delete(f"/api/datasets/{dataset_id}").status_code == 200
    assert fake.objects == {}


def test_failed_storage_fails_the_upload_and_records_nothing(client, monkeypatch):
    monkeypatch.setattr(
        api, "object_store", lambda config: ObjectStore(config, client=FakeS3(fail=True))
    )
    response = client.post("/api/datasets", files={"file": ("sales.csv", b"a,b\n1,2\n")})
    assert response.status_code == 502
    assert client.get("/api/datasets").json() == []
