import pytest

from bragi_server import client_api, storage
from bragi_server.youtube import clear_resolve_cache, clear_title_cache


@pytest.fixture(autouse=True)
def reset_performance_state(monkeypatch):
    clear_resolve_cache()
    clear_title_cache()
    storage.clear_storage_caches()
    monkeypatch.setattr(client_api, "_PREFETCH_ENABLED", False)
