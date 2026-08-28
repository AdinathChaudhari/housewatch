"""Shared pytest fixtures for the housewatch test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from housewatch import config as config_mod
from housewatch.config import Config


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    """A path to a (not-yet-existing) config.toml under tmp_path.

    Tests write to this with ``config.save(cfg, path)`` and/or read it back
    with ``config.load(path)``; the file starts absent so "no file yet"
    behavior is exercised the same way a fresh install would hit it.
    """
    return tmp_path / "housewatch" / "config.toml"


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """A path to a (not-yet-existing) housewatch.db under tmp_path."""
    return tmp_path / "housewatch" / "housewatch.db"


@pytest.fixture
def tmp_config(tmp_config_path: Path, tmp_db_path: Path) -> Config:
    """A valid Config pointed entirely at tmp_path (db + would-be config file).

    Convenience for tests in other modules (store.py, collector.py, ...)
    that just need *a* valid on-disk-safe Config without hand-building one.
    """
    cfg = config_mod.Config(db_path=str(tmp_db_path))
    config_mod.validate(cfg)
    return cfg
