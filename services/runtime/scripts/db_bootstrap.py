#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass

import psycopg
from psycopg import sql

from aegora_runtime.config import ConfigError, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Create target PostgreSQL role and database.")
    parser.add_argument("--admin-user", default=getpass.getuser())
    parser.add_argument("--admin-db", default="postgres")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    target_user = settings.postgres.user
    target_db = settings.postgres.database
    target_password = settings.postgres.password
    if not target_password:
        raise ConfigError("PG_PASSWORD 不能为空，创建目标 role 需要密码")

    conn = psycopg.connect(
        host=settings.postgres.host,
        port=settings.postgres.port,
        dbname=args.admin_db,
        user=args.admin_user,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target_user,))
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(target_user),
                        sql.Literal(target_password),
                    )
                )
                print(f"created role={target_user}")
            else:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(target_user),
                        sql.Literal(target_password),
                    )
                )
                print(f"updated role password={target_user}")

            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(target_db),
                        sql.Identifier(target_user),
                    )
                )
                print(f"created database={target_db}")
            else:
                cur.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(target_db),
                        sql.Identifier(target_user),
                    )
                )
                print(f"database exists={target_db}")
    finally:
        conn.close()

    ext_conn = psycopg.connect(
        host=settings.postgres.host,
        port=settings.postgres.port,
        dbname=target_db,
        user=args.admin_user,
        autocommit=True,
    )
    try:
        with ext_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            enable_optional_extension(cur, "pgcrypto")
            cur.execute(
                """
                SELECT default_version IS NOT NULL AS available
                FROM pg_available_extensions
                WHERE name = 'zhparser'
                """
            )
            row = cur.fetchone()
            if row and row[0]:
                cur.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
                print("extension zhparser=enabled")
            else:
                print("extension zhparser=unavailable")
            print("extension vector=enabled")
    finally:
        ext_conn.close()


def enable_optional_extension(cur, name: str) -> None:
    cur.execute(
        """
        SELECT default_version IS NOT NULL AS available
        FROM pg_available_extensions
        WHERE name = %s
        """,
        (name,),
    )
    row = cur.fetchone()
    if row and row[0]:
        cur.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
        print(f"extension {name}=enabled")
    else:
        print(f"extension {name}=unavailable")


if __name__ == "__main__":
    main()
