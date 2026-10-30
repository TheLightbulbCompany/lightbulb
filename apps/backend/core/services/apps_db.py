"""Reset an app's PREVIEW Postgres database to migrated schema + seed. Never touches live; no backups."""

import asyncio

import psycopg

TIMEOUT_S = 25  # R56: the reset runs inside a request, under API Gateway's 30 s


class AppsDbError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def _reset(database_url: str, migrations: list[str], seed_sql: str) -> None:
    # One connection, one transaction (autocommit off): any failure rolls everything back on exit.
    async with await psycopg.AsyncConnection.connect(database_url, connect_timeout=TIMEOUT_S) as conn:
        # Strings without params use the simple query protocol, so multi-statement files
        # (incl. Drizzle's `--> statement-breakpoint` comments) run as-is.
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for sql in (*migrations, seed_sql):
            await conn.execute(sql)


async def reset_preview(database_url: str, *, migrations: list[str], seed_sql: str) -> None:
    try:
        await asyncio.wait_for(_reset(database_url, migrations, seed_sql), TIMEOUT_S)
    except Exception as e:
        # Never forward str(e): psycopg echoes host/user/db. Class name + SQLSTATE only.
        detail = type(e).__name__ + (f" (SQLSTATE {e.sqlstate})" if getattr(e, "sqlstate", None) else "")
        raise AppsDbError("reset_failed", f"preview database reset failed: {detail}") from None
