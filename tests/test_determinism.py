"""Repeatability: the same payloads give the same data, independent of sharding."""

from conftest import checksums, new_client, run_all, small_payloads

from factory.data_engine.rng import checksum, fmt_checksum


def test_checksums_identical_across_shard_counts():
    a, _ = new_client()
    b, _ = new_client()
    run_all(a, small_payloads(shards=1), api=False)
    run_all(b, small_payloads(shards=5), api=False)
    assert checksums(a, "test-small") == checksums(b, "test-small")
    ma = a.get("/factory/runs/test-small/manifest").json()
    mb = b.get("/factory/runs/test-small/manifest").json()
    assert ma["data_spec_hash"] == mb["data_spec_hash"]  # shards are not part of the data spec


def test_replay_is_identical(generated):
    client, _, _ = generated
    again, _ = new_client()
    run_all(again, small_payloads(), api=False)
    assert checksums(again, "test-small") == checksums(client, "test-small")


def test_reseed_changes_rows_but_not_shape(generated):
    client, _, _ = generated
    other, _ = new_client()
    resp = run_all(other, small_payloads(seed=20261013), api=False)
    assert resp["s7"].json()["gate"] == "data_quality_gate PASSED"
    a, b = checksums(client, "test-small"), checksums(other, "test-small")
    assert a["member_demographics"] != b["member_demographics"]
    ma = client.get("/factory/runs/test-small/manifest").json()
    mb = other.get("/factory/runs/test-small/manifest").json()
    assert ma["entity_counts"]["member_demographics"] == mb["entity_counts"]["member_demographics"]
    assert ma["data_spec_hash"] != mb["data_spec_hash"]


def test_evolve_is_prefix_stable():
    """Growing members keeps the first N members byte-identical (evolve mode)."""
    small, reg_small = new_client()
    big, reg_big = new_client()
    run_all(small, small_payloads(600), api=False)
    run_all(big, small_payloads(800), api=False)
    sql = "SELECT * FROM member_demographics WHERE member_number <= 'MBR-0000600'"
    a = reg_small.runs["test-small"].stores.tables.query(sql)
    b = reg_big.runs["test-small"].stores.tables.query(sql)
    assert a.num_rows == 600 and fmt_checksum(checksum(a)) == fmt_checksum(checksum(b))


def test_failed_shard_is_retried_with_identical_result(monkeypatch):
    """A task that fails once (e.g. a transient store error) is re-run; upserts make the result identical."""
    from factory.data_engine.stages import s5_claims_auth

    clean, _ = new_client()
    run_all(clean, small_payloads(), api=False)

    real = s5_claims_auth.run_task
    calls = {"n": 0}

    def flaky(ctx, index, prep):
        calls["n"] += 1
        real(ctx, index, prep)          # writes part of the shard...
        if calls["n"] == 2:
            raise RuntimeError("simulated transient failure")  # ...then fails, so the task is retried

    monkeypatch.setattr(s5_claims_auth, "run_task", flaky)
    retried, _ = new_client()
    resp = run_all(retried, small_payloads(), api=False)
    assert resp["s5"].status_code == 200 and calls["n"] == 4  # 3 shards + 1 retry
    assert checksums(retried, "test-small") == checksums(clean, "test-small")
