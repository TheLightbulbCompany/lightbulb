"""Wave-2 dev acceptance harness (lane G; plans/wave2-README.md G1/G2, spec 09 §5.5, §8.3).

    cd apps/backend
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance run --scenarios A0-A13 [--workspace org_…] [--apply] [--report out.md]
    ENVIRONMENT=dev AWS_PROFILE=isol8-admin uv run python -m scripts.super_agent.acceptance gate [--workspace org_…] [--apply] --report ../../super-agent-design/lanes/gates/wave2.md

Dry-run without ``--apply``: prints what would run and touches nothing. Even a dry-run imports ``core.config``,
so ``apps/backend/.env`` must validate (at least ``CLERK_ISSUER``, e.g. the dev instance
``https://up-moth-55.clerk.accounts.dev``) with ``ENVIRONMENT=dev`` on the command line. One file per scenario,
``async def aNN(ctx) -> Result``; the registry in ``report.py`` names them. ``ctx.py`` holds ``Ctx``/``Result``.
One workspace (default: dev "Super Agent Test"); Sam = its owner, Priya = ``sa-test-2+clerk_test@…`` added as a member.
"""
