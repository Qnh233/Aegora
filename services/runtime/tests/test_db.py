from __future__ import annotations

from unittest.mock import patch

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect


def test_connect_passes_bounded_postgres_timeout() -> None:
    settings = load_settings(env_path=None)

    with patch("aegora_runtime.db.psycopg.connect") as mock_connect:
        with connect(settings):
            pass

    _, kwargs = mock_connect.call_args
    assert kwargs["connect_timeout"] == settings.postgres.connect_timeout_seconds
