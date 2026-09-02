from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from aegora_runtime.config import Settings, load_settings


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[Connection]:
    cfg = settings or load_settings(validate_secrets=True)
    with psycopg.connect(cfg.postgres.dsn, row_factory=dict_row) as conn:
        yield conn


def ping(settings: Settings | None = None) -> dict[str, object]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_database() AS database,
                    current_user AS user,
                    version() AS version
                """
            )
            row = cur.fetchone()
    return dict(row or {})

