"use client";

import { Suspense } from "react";
import { GatewayProvider } from "@/hooks/useGateway";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";

// Flag-gates the GatewayProvider wrap for /settings. `layout.tsx` is a server
// component and `useUnifiedNav` is a client hook, so the conditional wrap lives
// here.
//
// Flag-OFF (today's app): NO provider — /settings stays pure-REST, opening no
// idle WebSocket, byte-identical to the legacy surface.
//
// Flag-ON (unified nav): wrap in the same GatewayProvider the chat surface uses
// so the gateway-bound config panels (Skills, Skill Proposals, Dreams, Channel
// Status, MCP servers, the LLM health dot) can mount lazily.
function SettingsGatewayGateInner({
  children,
}: {
  children: React.ReactNode;
}) {
  const unifiedNav = useUnifiedNav();
  return unifiedNav ? <GatewayProvider>{children}</GatewayProvider> : <>{children}</>;
}

// `useUnifiedNav` reads `useSearchParams`, which Next.js 16 requires under a
// <Suspense> boundary — otherwise it de-opts the statically-exported
// /settings/* routes (e.g. /settings/connections) and the build's prerender
// fails. Children pass straight through the fallback so they still render
// during prerender (the gateway-bound panels are client-only + flag-on anyway).
export function SettingsGatewayGate({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <Suspense fallback={<>{children}</>}>
      <SettingsGatewayGateInner>{children}</SettingsGatewayGateInner>
    </Suspense>
  );
}
