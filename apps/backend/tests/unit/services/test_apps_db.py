import logging
import os
import socket
import subprocess
import time

import pytest

from core.services.apps_db import AppsDbError, reset_preview

IT = pytest.mark.skipif(os.environ.get("APPS_DB_IT") != "1", reason="set APPS_DB_IT=1 (needs docker)")
MIG = "CREATE TABLE t (id int primary key, v text);\n--> statement-breakpoint\nCREATE INDEX t_v ON t (v);\n"
SEED = "INSERT INTO t VALUES (1, 'a'), (2, 'b');"


async def test_errors_never_contain_the_url(caplog):
    caplog.set_level(logging.DEBUG)
    url = "postgresql://secretuser:s3cr3tpw@unroutable-host.invalid:5432/secretdb"
    with pytest.raises(AppsDbError) as e:
        await reset_preview(url, migrations=[MIG], seed_sql=SEED)
    assert e.value.code == "reset_failed"
    blob = str(e.value) + caplog.text
    for s in ("secretuser", "s3cr3tpw", "unroutable-host", "secretdb"):
        assert s not in blob


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pg_factory():
    names = []

    def start():
        port, name = _free_port(), f"appsdb-it-{os.getpid()}-{len(names)}"
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD=pw",
                "-p",
                f"{port}:5432",
                "postgres:18",
            ],
            check=True,
            capture_output=True,
        )
        names.append(name)
        url = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
        for _ in range(60):
            if (
                subprocess.run(
                    ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1"], capture_output=True
                ).returncode
                == 0
            ):
                time.sleep(1)
                return url
            time.sleep(1)
        raise RuntimeError("postgres not ready")

    yield start
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


async def _rows(url, sql="SELECT id FROM t ORDER BY id"):
    import psycopg

    async with await psycopg.AsyncConnection.connect(url) as c:
        return [r[0] for r in await (await c.execute(sql)).fetchall()]


@IT
async def test_reset_applies_migrations_then_seed(pg_factory):
    url = pg_factory()
    await reset_preview(url, migrations=[MIG], seed_sql=SEED)
    assert await _rows(url) == [1, 2]


@IT
async def test_reset_is_idempotent(pg_factory):
    url = pg_factory()
    for _ in range(2):
        await reset_preview(url, migrations=[MIG], seed_sql=SEED)
    assert await _rows(url) == [1, 2]


@IT
async def test_failed_reset_rolls_back(pg_factory):
    url = pg_factory()
    await reset_preview(url, migrations=[MIG], seed_sql=SEED)
    with pytest.raises(AppsDbError):
        await reset_preview(url, migrations=[MIG], seed_sql="INSERT INTO nope VALUES (1);")
    assert await _rows(url) == [1, 2]


@IT
async def test_preview_writes_never_reach_live(pg_factory):
    preview, live = pg_factory(), pg_factory()
    await reset_preview(live, migrations=[MIG], seed_sql="INSERT INTO t VALUES (9, 'live');")
    await reset_preview(preview, migrations=[MIG], seed_sql=SEED)
    assert await _rows(live) == [9]
