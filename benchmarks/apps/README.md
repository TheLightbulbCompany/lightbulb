# Apps builder eval

Scores any app builder on 20 tasks (task directories, runner, isolated serving, probes and judge
are added in later tasks). Spec: `docs/superpowers/specs/2026-10-28-apps-design.md` section 6.

Standalone package, not in the root pnpm workspace:

    pnpm install --ignore-workspace
    pnpm vitest run
    pnpm exec playwright --version

`vendor/dyad/` holds Dyad's app-builder specs and journeys (Apache-2.0, commit 0ee4a524) as
source material; see `NOTICE`.
