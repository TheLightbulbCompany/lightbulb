import { Suspense } from "react";
import { GatewayProvider } from "@/hooks/useGateway";
import { TeamsLayout } from "@/components/teams/TeamsLayout";
import { UnifiedNavRedirect } from "@/components/teams/UnifiedNavRedirect";

export default function Layout({ children }: { children: React.ReactNode }) {
  // GatewayProvider opens the WS to API Gateway lazily — already used by /chat
  // and MyChannelsSection. /teams needs it too so TeamsEventsProvider can send
  // teams.subscribe and listen for live events.
  return (
    <GatewayProvider>
      {/* Flag-gated Phase E: send unified-nav users to /chat (the absorbed
          surface). Suspense-wrapped — useUnifiedNav reads useSearchParams. */}
      <Suspense fallback={null}>
        <UnifiedNavRedirect />
      </Suspense>
      <TeamsLayout>{children}</TeamsLayout>
    </GatewayProvider>
  );
}
