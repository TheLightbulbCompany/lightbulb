// apps/frontend/src/components/teams/cases/caseNav.ts

// Stands in for upstream Paperclip's `useCaseHref()` (paperclip/ui/src/lib/router):
// in the Teams shell a case opens as the `teams-case-detail` panel, addressed by
// its identifier (`LB-C7`) or uuid, and the list is the `teams-cases` panel.

import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";

export function caseHref(idOrIdentifier?: string): string {
  return idOrIdentifier ? teamsDetailUrl("case", idOrIdentifier) : teamsPanelUrl("cases");
}

export function issueHref(idOrIdentifier: string): string {
  return teamsDetailUrl("issue", idOrIdentifier);
}
