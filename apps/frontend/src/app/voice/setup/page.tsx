"use client";

import { Onboarding } from "@/components/voice/Onboarding";
import { useAgentConfig } from "@/components/voice/data";

// The first-run flow on its own, to set the assistant up again; /voice opens it for a workspace with no Agent page yet.
export default function Page() {
  const { data: config, save } = useAgentConfig();
  return config ? <Onboarding config={config} save={save} /> : <div className="vx-page"><p className="vx-empty">Loading…</p></div>;
}
