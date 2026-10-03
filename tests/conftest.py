import shutil
from pathlib import Path

import pytest

from cairn.config import load_config
from cairn.indexer import build_index
from cairn.store import Store

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "vault"


@pytest.fixture
def vault(tmp_path):
    dst = tmp_path / "vault"
    shutil.copytree(EXAMPLE, dst)
    return dst


@pytest.fixture
def indexed(vault):
    cfg = load_config(vault)
    store = Store(cfg.db_path)
    build_index(cfg, store)
    yield cfg, store
    store.close()
