import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "demo" / "shop"
sys.path.insert(0, str(SHOP))

from server import Shop  # noqa: E402


@pytest.fixture
def project(tmp_path):
    """A copy of the demo shop project: spec, config, flows (the database is created by the shop)."""
    dest = tmp_path / "shop"
    shutil.copytree(SHOP, dest, ignore=shutil.ignore_patterns("__pycache__", "server.py", "*.db"))
    return dest


@pytest.fixture
def shop(project, monkeypatch):
    s = Shop(project / "shop.db", 0)
    monkeypatch.setenv("SHOP_URL", s.url)
    monkeypatch.setenv("SHOP_TOKEN", "test-token")
    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    yield s
    s.close()
