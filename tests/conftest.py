"""Pytest fixtures: isolated SQLite database and TestClient per test."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    tmpdir = tempfile.mkdtemp(prefix="gapcheck-test-")
    db_path = Path(tmpdir) / "test.db"
    monkeypatch.setenv("GAPCHECK_DB_PATH", str(db_path))

    # Import after the env var is set so config.DEFAULT_DB_PATH picks it up.
    import importlib

    from app import config, db, main

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c
