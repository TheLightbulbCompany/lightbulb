"use client";

// The Work place's Threads tab: every thread the viewer can see in the workspace, grouped.
// Opens one in the right pane. Only meaningful with the switch on; a stale link without it
// gets one line, not today's Tasks board.

import { useEffect, type ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { openTaskPane } from "@/components/workspace/workspaceController";
import { TaskOverview } from "./TaskOverview";

export function ThreadsPanel(): ReactElement {
  const on = useSuperAgent();
  const { userId } = useAuth();
  useEffect(() => { if (on) capture("overview_opened", { source: "work" }); }, [on]);
  if (!on) return <p className="px-6 py-4 text-ui text-muted-foreground">Threads aren&apos;t on for this workspace.</p>;
  return <div className="px-6 py-4"><TaskOverview scope={{ kind: "workspace" }} viewerId={userId ?? ""} onOpen={openTaskPane} /></div>;
}
