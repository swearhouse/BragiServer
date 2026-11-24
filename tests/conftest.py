from unittest.mock import patch

import pytest

from bragi_server import client_api, storage
from bragi_server.youtube import clear_resolve_cache, clear_title_cache


@pytest.fixture(autouse=True)
def reset_performance_state(monkeypatch):
    clear_resolve_cache()
    clear_title_cache()
    storage.clear_storage_caches()
    monkeypatch.setattr(client_api, "_PREFETCH_ENABLED", False)


@pytest.fixture(autouse=True)
def no_youtube_player_response():
    with patch("bragi_server.youtube._fetch_player_response", return_value=None):
        yield
