# G1 baseline runs

One row per run of `scenarios/G1.yaml` (14 room cases from 42 real agent failures, 7–28 Oct 2026).
Run it before and after an agent change and add a row; read each failing reply before blaming the agent.

| date | image | pass | failing cases |
|---|---|---|---|
| 2026-10-30 | h-2026.10.30-b58b0d6 (ask_person, lightbulb_board) | 11/14 | **no-false-done**: "Sent the five-bullet summary to aden@example.com just now". No send in the dev mail logs for that case: a false "done". **right-task-only**: picked the right task but "the board refuses status changes … room messages carry no run to attribute the write to" (chat turns carry no Paperclip run, F2 fix 7). answer-the-question: the reply was fine ("No — there's no weekly digest routine running…"); the rubric demanded one line of why. Loosened after this run; not re-run yet. |

How it ran: a fresh dev fixture workspace (`scripts.load.fixtures setup --orgs 1 --users-per-org 1`), then
`MP_ORG_ID=<its org> uv run python -m scripts.multiparty_e2e.runner run scripts/multiparty_e2e/scenarios/G1.yaml`,
then remove the `mp-e2e-*` members from that org (teardown refuses an org with other members) and run `scripts.load.fixtures teardown`. The default `MP_ORG_ID` ("Jev Listening Test") no longer exists in dev Clerk.
