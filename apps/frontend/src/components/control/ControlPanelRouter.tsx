"use client";

import { useEffect } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { OverviewPanel } from "./panels/OverviewPanel";
import { SessionsPanel } from "./panels/SessionsPanel";
import { UsagePanel } from "./panels/UsagePanel";
import { CronPanel } from "./panels/CronPanel";
import { AgentsPanel } from "./panels/AgentsPanel";
import { SkillsPanel } from "./panels/SkillsPanel";
import { LLMPanel } from "./panels/LLMPanel";
import { CreditsPanel } from "./panels/CreditsPanel";


interface ControlPanelRouterProps {
  panel: string;
  onPanelChange?: (panel: string) => void;
}

type UserMeResponse = {
  provider_choice?: "chatgpt_oauth" | "byo_key" | "bedrock_claude" | null;
};

const PANELS: Record<string, React.ComponentType> = {
  overview: OverviewPanel,
  sessions: SessionsPanel,
  usage: UsagePanel,
  cron: CronPanel,
  agents: AgentsPanel,
  skills: SkillsPanel,
  llm: LLMPanel,
  credits: CreditsPanel,
};

export function ControlPanelRouter({ panel, onPanelChange }: ControlPanelRouterProps) {
  const api = useApi();
  const { data: me } = useSWR<UserMeResponse>(
    "/users/me",
    () => api.get("/users/me") as Promise<UserMeResponse>,
  );

  // Defense-in-depth: the sidebar already hides the Credits item for
  // non-Bedrock users, but if the parent's panel state still reads
  // "credits" (URL param, stale prop), fall back to the overview rather
  // than render a panel the user isn't supposed to see.
  const shouldFallbackFromCredits =
    panel === "credits" && me !== undefined && me.provider_choice !== "bedrock_claude";
  const resolvedPanel = shouldFallbackFromCredits ? "overview" : panel;

  // Sync the parent's panel state so the URL + sidebar match what's actually
  // rendered. Without this, a non-Bedrock user landing on ?panel=credits sees
  // OverviewPanel, but the URL stays at ?panel=credits and no sidebar item
  // highlights — back/refresh keeps reopening the invalid panel. Codex P2 on
  // PR #479.
  useEffect(() => {
    if (shouldFallbackFromCredits) onPanelChange?.("overview");
  }, [shouldFallbackFromCredits, onPanelChange]);

  // LLMPanel and OverviewPanel both take the panel-switch callback for
  // their deep-link affordances (LLM → Credits; Overview → Cron/Skills/etc).
  // Every other panel takes no props.
  if (resolvedPanel === "llm") {
    return <LLMPanel onPanelChange={onPanelChange} />;
  }
  if (resolvedPanel === "overview") {
    return <OverviewPanel onPanelChange={onPanelChange} />;
  }

  const Panel = PANELS[resolvedPanel] || PANELS.overview;
  return <Panel />;
}
