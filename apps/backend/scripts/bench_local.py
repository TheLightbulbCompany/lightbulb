"""The BEAM runner's local backend: only the bench routes, on localhost (never merged).

    AWS_PROFILE=... ENVIRONMENT=local AGENTCORE_MEMORY_ID=<a dev memory> BENCH_LOCAL_TOKEN=... \\
        uv run uvicorn scripts.bench_local:app --port 8765

main.py's lifespan starts sweepers (memory sync, digests, emails, idle stops) that would act on
whatever tables the AWS credentials reach; this app starts none of them. The routes are the same
module main.py mounts.
"""

from fastapi import FastAPI

from core.config import settings
from routers import internal_bench

if settings.ENVIRONMENT != "local" or "prod" in settings.AGENTCORE_MEMORY_ID.lower():
    raise SystemExit("bench_local runs with ENVIRONMENT=local and a non-prod AGENTCORE_MEMORY_ID only")

app = FastAPI()
app.include_router(internal_bench.router, prefix="/api/v1/internal/bench")
