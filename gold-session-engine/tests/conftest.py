import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data directory so nothing touches ./data."""
    monkeypatch.setenv("GSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GSE_CONFIG", str(tmp_path / "settings.yaml"))
    yield
