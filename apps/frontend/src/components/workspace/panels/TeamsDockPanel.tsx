"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { useEffect, useState } from "react";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";
import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import {
  ProvisioningOverlay,
  SubscribeOverlay,
  ErrorOverlay,
} from "@/components/teams/TeamsStatusOverlays";

interface TeamsDockPanelParams {
  teamsKey: string; // "inbox" | "issues" | … | "issue-detail" | …
  detailId?: string;
  agent?: string; // durable issues filter
  new?: string; // one-shot, re-delivered via updateParameters
  newIssue?: string; // one-shot
}

export function TeamsDockPanel(props: IDockviewPanelProps<TeamsDockPanelParams>): React.ReactElement {
  // Real hook shape (useTeamsApi.ts:158-184): a discriminated union keyed on
  // `kind` — "loading" | "ready" | "provisioning" | "subscribe_required" |
  // { kind: "error"; error }. Mirror InlineTeamsPanel.tsx:47-50 EXACTLY:
  // provisioning / subscribe_required / error surface an overlay; loading and
  // ready both fall through to the router.
  const status = useTeamsWorkspaceStatus();

  // One-shots re-deliver via updateParameters on an already-open panel —
  // subscribe to parameter changes so a second ?new=task command re-fires.
  const [params, setParams] = useState(props.params);
  useEffect(() => {
    const d = props.api.onDidParametersChange((next) => setParams(next as TeamsDockPanelParams));
    return () => d.dispose();
  }, [props.api]);

  if (status.kind === "provisioning") return <ProvisioningOverlay />;
  if (status.kind === "subscribe_required") return <SubscribeOverlay />;
  if (status.kind === "error") return <ErrorOverlay error={status.error} />;

  const clearOneShot = (key: "new" | "newIssue") =>
    props.api.updateParameters({ [key]: undefined });

  return (
    <div className="editorial-page h-full overflow-auto">
      <TeamsPanelRouter
        panel={params.teamsKey}
        detailId={params.detailId ?? null}
        panelParams={{
          ...(params.agent ? { agent: params.agent } : {}),
          ...(params.new ? { new: params.new } : {}),
          ...(params.newIssue ? { newIssue: params.newIssue } : {}),
        }}
        onConsumeOneShot={clearOneShot}
        onUpdateDurableParam={(key, value) =>
          props.api.updateParameters({ [key]: value ?? undefined })
        }
      />
    </div>
  );
}
