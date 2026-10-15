"""Repeatability guarantees from the spec: same blueprint + seed => same checksums, independent of sharding."""

from conftest import fresh_stores, small_blueprint

from factory.data_engine.rng import checksum, fmt_checksum
from factory.data_engine.runner import run_data_engine


def test_checksums_identical_across_shard_counts():
    bp = small_blueprint()
    a = run_data_engine(bp, fresh_stores(), shards=1).manifest.entity_checksums
    b = run_data_engine(bp, fresh_stores(), shards=5).manifest.entity_checksums
    assert a == b


def test_replay_is_identical(generated):
    bp, _, first = generated
    again = run_data_engine(bp, fresh_stores(), shards=3)
    assert again.manifest.entity_checksums == first.manifest.entity_checksums
    assert again.manifest.data_spec_hash == first.manifest.data_spec_hash


def test_reseed_changes_rows_but_not_shape(generated):
    bp, _, first = generated
    reseeded = small_blueprint(**{"repeatability.root_seed": bp.repeatability.root_seed + 1})
    run = run_data_engine(reseeded, fresh_stores(), shards=3)
    assert run.manifest.entity_checksums["member_demographics"] != first.manifest.entity_checksums["member_demographics"]
    assert run.manifest.entity_counts["member_demographics"] == first.manifest.entity_counts["member_demographics"]
    assert run.passed


def test_evolve_is_prefix_stable():
    """Growing members keeps the first N members byte-identical (evolve mode)."""
    small, big = fresh_stores(), fresh_stores()
    run_data_engine(small_blueprint(600), small, shards=2)
    run_data_engine(small_blueprint(800), big, shards=4)
    sql = "SELECT * FROM member_demographics WHERE member_number <= 'MBR-0000600'"
    assert fmt_checksum(checksum(small.tables.query(sql))) == fmt_checksum(checksum(big.tables.query(sql)))


def test_blueprint_hash_changes_with_infra_but_data_hash_does_not():
    a = small_blueprint()
    b = small_blueprint(**{"infra_spec.region": "us-east4", "tenant.id": "ma-demo-02"})
    assert a.blueprint_hash != b.blueprint_hash
    assert a.data_spec_hash == b.data_spec_hash
