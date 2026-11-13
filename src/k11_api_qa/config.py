"""Project config: ``.qa-mcp/api-test.yaml``.

    spec: openapi.yaml                       # used for schema checks and negative tests
    default_environment: local
    environments:
      local:   { base_url: http://localhost:8080 }
      staging: { base_url: https://staging.example.com, allow_writes: true }
      prod:    { base_url: https://api.example.com }        # read-only: writes refused
    auth:
      default: { type: bearer, token: "${ENV:API_TOKEN}" }
      # basic: {username, password} · api_key: {header, value} · oauth2: {token_url, client_id, client_secret, scope}
    databases:
      main: { url: "${ENV:TEST_DB_URL}", allow_writes: true, mask: [email] }
    fixtures:
      customer:
        db: main
        insert: { table: customers, values: { id: "${uuid}", email: "${random_email}" } }
        delete: { table: customers, where: { id: "${id}" } }

Writes (POST/PUT/PATCH/DELETE, and database writes for fixtures/cleanup) are refused on an
environment unless it is local, its name looks like a test environment, or it sets
``allow_writes: true``. Environments named like production never allow writes unless they
also set ``production_writes_acknowledged: true``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

CONFIG = ".qa-mcp/api-test.yaml"
PROD_NAME = re.compile(r"(^|[-_.])(prod|production|live|prd)([-_.]|$)", re.I)
TEST_NAME = re.compile(r"(^|[-_.])(local|dev|test|qa|ci|staging|stage|sandbox|preview|uat)([-_.]|$)", re.I)
LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0|.*\.(localhost|test|local))$", re.I)


@dataclass
class Config:
    root: Path
    spec: str | None = None
    default_environment: str = "local"
    environments: dict[str, dict] = field(default_factory=dict)
    auth: dict[str, dict] = field(default_factory=dict)
    databases: dict[str, dict] = field(default_factory=dict)
    fixtures: dict[str, dict] = field(default_factory=dict)
    timeout_s: float = 30

    def environment(self, name: str | None) -> tuple[str, dict]:
        name = name or self.default_environment
        if name not in self.environments:
            if not self.environments and name == "local":
                return name, {"base_url": "http://localhost:8080"}
            raise ValueError(f"Unknown environment '{name}'. Known: {sorted(self.environments) or ['(none)']}")
        return name, self.environments[name]


def load(root: Path) -> Config:
    path = Path(root) / CONFIG
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text("utf8")) or {}
    return Config(root=Path(root), spec=data.get("spec"), default_environment=data.get("default_environment", "local"),
                  environments=data.get("environments") or {}, auth=data.get("auth") or {},
                  databases=data.get("databases") or {}, fixtures=data.get("fixtures") or {},
                  timeout_s=float(data.get("timeout_s", 30)))


def writes_allowed(env_name: str, env: dict, base_url: str) -> tuple[bool, str]:
    """Whether data-changing calls may run against this environment, and why."""
    if PROD_NAME.search(env_name):
        if env.get("allow_writes") and env.get("production_writes_acknowledged"):
            return True, "production writes explicitly acknowledged"
        return False, f"'{env_name}' looks like production; writes are refused"
    if "allow_writes" in env:
        return bool(env["allow_writes"]), ("allow_writes: true" if env["allow_writes"] else "allow_writes: false")
    host = urlsplit(base_url).hostname or ""
    if LOCAL_HOST.match(host):
        return True, "local host"
    if TEST_NAME.search(env_name):
        return True, f"'{env_name}' is a test environment"
    return False, f"'{env_name}' is not marked as a test environment; set allow_writes: true to permit writes"


def summary(cfg: Config) -> dict:
    envs = {}
    for name, env in cfg.environments.items():
        ok, why = writes_allowed(name, env, str(env.get("base_url", "")))
        envs[name] = {"base_url": env.get("base_url"), "writes": ok, "why": why, "auth": env.get("auth", "default")}
    return {"config": CONFIG if (cfg.root / CONFIG).is_file() else None, "spec": cfg.spec,
            "default_environment": cfg.default_environment, "environments": envs,
            "auth_profiles": {k: v.get("type", "bearer") for k, v in cfg.auth.items()},
            "databases": {k: {"allow_writes": bool(v.get("allow_writes")), "mask": v.get("mask", [])} for k, v in cfg.databases.items()},
            "fixtures": sorted(cfg.fixtures)}
