"""Wave-2 dev acceptance harness (lane G; plans/wave2-README.md G1/G2, spec 09 §5.5, §8.3).

    cd apps/backend
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance setup   [--apply]
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance run --scenarios A0-A13 [--apply] [--report out.md]
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance gate  [--apply] --report ../../super-agent-design/lanes/gates/wave2.md
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance teardown [--apply]

Dry-run without ``--apply``: prints what would run and touches nothing. One file per scenario,
``async def aNN(ctx) -> Result``; the registry in ``report.py`` names them. ``ctx.py`` holds ``Ctx``/``Result``.
"""
