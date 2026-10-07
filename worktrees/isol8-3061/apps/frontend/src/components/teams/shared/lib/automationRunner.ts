import type { CompanyAgent } from "@/components/teams/shared/types";

/**
 * Which agent the backend will actually run a seeded automation as.
 *
 * A **mirror** of `_find_friday_id` in `company_deploy_service.py`, which
 * `inbound_triggers.seed_trigger_routine` calls to pick the routine's agent:
 * the apex CEO is the `role === "ceo"` agent that is ROOTLESS (no `reportsTo`),
 * falling back to the first `ceo` if — impossibly — none are. After a company
 * deploy the package's own CEO also exists, as a division head reporting to
 * the workspace root CEO, so "first ceo" alone is not safe.
 *
 * Deliberately does NOT consult the `seeded` stamp. `seeded` comes from the
 * BFF's persisted `seeded_agent_ids` (`get_seeded_agent_ids`), which is
 * fail-OPEN: it is empty for a legacy company provisioned before those IDs were
 * recorded, and empty during any DynamoDB blip. The backend never looks at it
 * when choosing the runner, so neither may this — otherwise the UI names no
 * runner (and skips the mailbox-conflict warning) for exactly the automation
 * the backend still creates for the rootless CEO.
 *
 * `resolveCeoAgentId` in `getstarted/getStartedFirstTask.ts` answers a
 * different question — "may we pre-assign the user's own first task to this
 * agent" — where a name/role imposter must never match and matching nothing is
 * a safe outcome. Keep them separate.
 */
export function resolveAutomationRunner(
  agents: CompanyAgent[],
): CompanyAgent | undefined {
  const ceos = agents.filter((a) => a.role === "ceo");
  return ceos.find((a) => !a.reportsTo) ?? ceos[0];
}
