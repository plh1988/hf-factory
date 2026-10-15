import warnings

import pytest

from factory.adapters import build_stores
from factory.config import load_profile
from factory.contracts import Blueprint
from factory.data_engine.runner import run_data_engine

warnings.filterwarnings("ignore", category=DeprecationWarning)

BLUEPRINT = "blueprints/s1_dev.json"


def small_blueprint(members: int = 600, **overrides) -> Blueprint:
    bp = Blueprint.load(BLUEPRINT)
    data = bp.model_dump(mode="json")
    data["data_spec"]["volumes"] = {"members": members, "providers": 30, "claims": members, "authorizations": members // 20}
    for path, value in overrides.items():
        node = data
        *head, last = path.split(".")
        for k in head:
            node = node[k]
        node[last] = value
    return Blueprint.model_validate(data)


def fresh_stores():
    return build_stores(load_profile("memory-offline"))


@pytest.fixture(scope="session")
def generated():
    """One small S1 run shared by read-only tests."""
    bp = small_blueprint()
    stores = fresh_stores()
    run = run_data_engine(bp, stores, shards=3)
    return bp, stores, run
