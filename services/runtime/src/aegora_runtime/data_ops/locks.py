from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from aegora_runtime.config import Settings
from aegora_runtime.db import connect


@contextmanager
def advisory_job_lock(settings: Settings, name: str) -> Iterator[bool]:
    """Try to hold a PG advisory lock for one data-ops job."""
    lock_key = f"data_ops:{name}"
    acquired = False
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (lock_key,))
            acquired = bool(cur.fetchone()["acquired"])
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
                conn.commit()
