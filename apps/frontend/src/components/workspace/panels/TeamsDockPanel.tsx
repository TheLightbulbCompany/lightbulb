"use client";

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";
import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import {
  ProvisioningOverlay,
  SubscribeOverlay,
  ErrorOverlay,
} from "@/components/teams/TeamsStatusOverlays";
import type { PanelProps } from "../panelComponents";

export function TeamsDockPanel(props: PanelProps): React.ReactElement {
  // Real hook shape (useTeamsApi.ts): a discriminated union keyed on
  // `kind` — "loading" | "ready" | "provisioning" | "subscribe_required" |
  // { kind: "error"; error }. provisioning / subscribe_required / error
  // surface an overlay; loading and ready both fall through to the router.
  const status = useTeamsWorkspaceStatus();

  if (status.kind === "provisioning") return <ProvisioningOverlay />;
  if (status.kind === "subscribe_required") return <SubscribeOverlay />;
  if (status.kind === "error") return <ErrorOverlay error={status.error} />;

  return (
    // workspace-panel-page (workspace.css): this wrapper IS the panel's
    // scroll container — it collapses editorial-page's viewport min-height
    // to the slot and lets descendants size against the slot (cqh).
    <div className="editorial-page workspace-panel-page h-full overflow-auto">
      <TeamsPanelRouter panel={String(props.params.teamsKey ?? "")} />
    </div>
  );
}
