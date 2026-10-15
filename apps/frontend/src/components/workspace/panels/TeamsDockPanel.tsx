"use client";

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";
import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import {
  ProvisioningOverlay,
  SubscribeOverlay,
  ErrorOverlay,
} from "@/components/teams/TeamsStatusOverlays";
import { replaceSearchParams } from "@/lib/shellNav";
import { clearDurableParamFromUrl } from "../urlChannel";
import type { PanelProps } from "../panelComponents";
import { updatePanelParams } from "../workspaceController";
import { WorkTabs } from "./WorkTabs";
import { isWorkTeamsKey } from "../panelRegistry";

interface TeamsDockPanelParams {
  teamsKey: string; // "inbox" | "issues" | … | "issue-detail" | …
  detailId?: string;
  agent?: string; // durable issues filter
  groupBy?: string; // durable Tasks grouping (the absorbed Goals/Projects keys)
  view?: string; // durable Agents view (the absorbed org-chart key)
  day?: string; // durable money day scope
  section?: string; // durable Money / Inbox section (the absorbed keys)
  new?: string; // one-shot, re-delivered via updateParameters
  newIssue?: string; // one-shot
}

export function TeamsDockPanel(props: PanelProps): React.ReactElement {
  // Real hook shape (useTeamsApi.ts:158-184): a discriminated union keyed on
  // `kind` — "loading" | "ready" | "provisioning" | "subscribe_required" |
  // { kind: "error"; error }. Mirror InlineTeamsPanel.tsx:47-50 EXACTLY:
  // provisioning / subscribe_required / error surface an overlay; loading and
  // ready both fall through to the router.
  const status = useTeamsWorkspaceStatus();

  // One-shots re-deliver as fresh params on an already-open panel, so a
  // second ?new=task command re-fires.
  const params = props.params as unknown as TeamsDockPanelParams;

  if (status.kind === "provisioning") return <ProvisioningOverlay />;
  if (status.kind === "subscribe_required") return <SubscribeOverlay />;
  if (status.kind === "error") return <ErrorOverlay error={status.error} />;

  const clearOneShot = (key: "new" | "newIssue") =>
    updatePanelParams(props.id, { [key]: undefined });

  return (
    // workspace-panel-page (workspace.css): this wrapper IS the panel's
    // scroll container — it collapses editorial-page's viewport min-height
    // to the slot and lets descendants size against the slot (cqh).
    <div className="editorial-page workspace-panel-page h-full overflow-auto">
      {!params.detailId && isWorkTeamsKey(params.teamsKey) && <WorkTabs teamsKey={params.teamsKey} />}
      <TeamsPanelRouter
        panel={params.teamsKey}
        detailId={params.detailId ?? null}
        panelParams={{
          ...(params.agent ? { agent: params.agent } : {}),
          ...(params.groupBy ? { groupBy: params.groupBy } : {}),
          ...(params.view ? { view: params.view } : {}),
          ...(params.day ? { day: params.day } : {}),
          ...(params.section ? { section: params.section } : {}),
          ...(params.new ? { new: params.new } : {}),
          ...(params.newIssue ? { newIssue: params.newIssue } : {}),
        }}
        onConsumeOneShot={clearOneShot}
        onUpdateDurableParam={(key, value) => {
          updatePanelParams(props.id, { [key]: value ?? undefined });
          // Dismissing a durable filter must also strip it from the URL when
          // the current URL command names this panel — otherwise a refresh
          // re-executes the command and resurrects the dismissed filter.
          if (value === null) {
            const cleared = clearDurableParamFromUrl(
              window.location.search,
              props.id,
              key,
            );
            if (cleared !== null) replaceSearchParams(new URLSearchParams(cleared));
          }
        }}
      />
    </div>
  );
}
