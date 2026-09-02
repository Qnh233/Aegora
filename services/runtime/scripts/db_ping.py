#!/usr/bin/env python3
from __future__ import annotations

from aegora_runtime.db import ping


def main() -> None:
    info = ping()
    print("PG connection ok")
    print(f"database={info['database']}")
    print(f"user={info['user']}")
    print(f"version={str(info['version']).splitlines()[0]}")


if __name__ == "__main__":
    main()

