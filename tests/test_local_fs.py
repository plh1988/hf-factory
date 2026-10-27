"""File-based blob store and text cache (memory-local profile)."""

import pytest
from conftest import run_all, small_payloads
from fastapi.testclient import TestClient

from factory.adapters import build_stores
from factory.adapters.local_fs import FileBlobStore, FileTextCache
from factory.config import AdapterConfig, load_profile
from factory.control.registry import Registry
from factory.control.server import create_control_app


def test_blob_store_round_trip_and_tenant_folders(tmp_path):
    a = FileBlobStore(AdapterConfig(kind="local", root=str(tmp_path)), "Tenant-A")
    b = FileBlobStore(AdapterConfig(kind="local", root=str(tmp_path)), "tenant-b")
    uri = a.put("docs/pa_policy/x1.md", b"# PA policy", "text/markdown")
    a.put("manifest/1.json", b"{}")
    assert uri.startswith("file:") and (tmp_path / "tenant-a" / "docs" / "pa_policy" / "x1.md").read_bytes() == b"# PA policy"
    assert a.get("docs/pa_policy/x1.md") == b"# PA policy"
    assert a.list() == ["docs/pa_policy/x1.md", "manifest/1.json"] and a.list("docs/") == ["docs/pa_policy/x1.md"]
    assert b.list() == []
    with pytest.raises(KeyError):
        b.get("docs/pa_policy/x1.md")


@pytest.mark.parametrize("key", ["../escape.txt", "docs/../../x", "/abs/path", "C:/x", "a//b", ""])
def test_blob_store_rejects_unsafe_keys(tmp_path, key):
    store = FileBlobStore(AdapterConfig(kind="local", root=str(tmp_path)), "t")
    with pytest.raises(ValueError):
        store.put(key, b"x")


def test_text_cache_survives_a_new_instance(tmp_path):
    cfg = AdapterConfig(kind="local", root=str(tmp_path / "cache"))
    FileTextCache(cfg).put("ab12", "generated text")
    assert FileTextCache(cfg).get("ab12") == "generated text"
    assert FileTextCache(cfg).get("missing") is None


def test_memory_local_profile_uses_files():
    stores = build_stores(load_profile("memory-local"), namespace="t1")
    assert type(stores.blobs).__name__ == "FileBlobStore" and stores.blobs.base.parts[-2:] == ("out", "t1")
    assert type(stores.text_cache).__name__ == "FileTextCache"


def test_pipeline_writes_documents_and_cache_to_disk(tmp_path):
    profile = load_profile("memory-offline")
    profile = profile.model_copy(update={"blobs": AdapterConfig(kind="local", root=str(tmp_path)),
                                         "text_cache": AdapterConfig(kind="local", root=str(tmp_path / "cache"))})
    client = TestClient(create_control_app(Registry(profile)))
    resp = run_all(client, small_payloads(members=200, tenant="files"))
    assert all(r.status_code == 200 for r in resp.values()), {k: r.json() for k, r in resp.items() if r.status_code != 200}
    docs = list((tmp_path / "files" / "docs").rglob("*.md"))
    assert docs and all(d.read_text(encoding="utf-8").strip() for d in docs)
    assert (tmp_path / "files" / "reports" / "rule-report.json").exists()
    assert len(list((tmp_path / "cache").glob("*.txt"))) >= len(docs)
