"""An app's Postgres databases: reset the PREVIEW to migrated schema + seed; apply new migrations to
the LIVE one before a production build. No backups (Neon's job)."""

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


def _detail(e: Exception) -> str:
    # Never forward str(e): psycopg echoes host/user/db. Class name + SQLSTATE only.
    return type(e).__name__ + (f" (SQLSTATE {e.sqlstate})" if getattr(e, "sqlstate", None) else "")


async def reset_preview(database_url: str, *, migrations: list[str], seed_sql: str) -> None:
    try:
        await asyncio.wait_for(_reset(database_url, migrations, seed_sql), TIMEOUT_S)
    except Exception as e:
        raise AppsDbError("reset_failed", f"preview database reset failed: {_detail(e)}") from None


async def _apply(database_url: str, migrations: list[str]) -> None:
    # One connection, one transaction: a failing migration leaves the live schema as it was.
    async with await psycopg.AsyncConnection.connect(database_url, connect_timeout=TIMEOUT_S) as conn:
        for sql in migrations:
            await conn.execute(sql)


async def apply_live(database_url: str, *, migrations: list[str]) -> None:
    """Run the not-yet-published migrations on the LIVE database, in order. The backend does this
    before the production build, so the build is only `next build` and the app's drizzle config
    needs no credentials (the starter ships none; the first publish on Demo Lab failed on that)."""
    try:
        await asyncio.wait_for(_apply(database_url, migrations), TIMEOUT_S)
    except Exception as e:
        raise AppsDbError("migrate_failed", f"live database migration failed: {_detail(e)}") from None
