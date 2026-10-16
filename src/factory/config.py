"""One environment variable picks the profile: FACTORY_PROFILE=memory | memory-offline | gcp.

Every backend choice lives in config/<profile>.yaml. Nothing else in the codebase reads
the profile; adapters are built from it in factory.adapters.build_stores().
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from factory.contracts import S2Output

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

ProfileName = Literal["memory", "memory-offline", "gcp"]


class Settings(BaseSettings):
    profile: ProfileName = "memory-offline"
    s2_json: str | None = None  # FACTORY_S2_JSON: s2 output inline (Cloud Run tasks/services get it this way)
    config_dir: Path | None = None  # FACTORY_CONFIG_DIR: where profile yaml files live when installed (e.g. /app/config)
    model_config = SettingsConfigDict(env_prefix="FACTORY_", env_file=REPO_ROOT / ".env", extra="ignore")


class AdapterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str

    def get(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


class Profile(BaseModel):
    name: str
    tables: AdapterConfig
    blobs: AdapterConfig
    vectors: AdapterConfig
    events: AdapterConfig
    text_cache: AdapterConfig
    deployer: AdapterConfig
    llm: AdapterConfig
    executor: AdapterConfig = AdapterConfig(kind="local")
    limits: dict[str, int] = {}  # e.g. max_members / max_claims for in-memory profiles


_PLACEHOLDER = re.compile(r"\$\{s2\.([a-z_]+)\}")  # ${s2.__json__} = the whole s2 output


def _resolve(value: Any, s2: S2Output | None) -> Any:
    """Replace ${s2.field} placeholders with values from the s2 output."""
    if isinstance(value, dict):
        return {k: _resolve(v, s2) for k, v in value.items()}
    if isinstance(value, str) and _PLACEHOLDER.search(value):
        if s2 is None:
            raise ValueError(f"profile needs an s2 output to resolve {value!r}")
        return _PLACEHOLDER.sub(lambda m: s2.model_dump_json() if m.group(1) == "__json__" else str(getattr(s2, m.group(1))), value)
    return value


def load_profile(name: str | None = None, s2: S2Output | None = None) -> Profile:
    settings = Settings()
    name = name or settings.profile
    path = (settings.config_dir or CONFIG_DIR) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no profile file {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Profile.model_validate({"name": name, **_resolve(raw, s2)})
