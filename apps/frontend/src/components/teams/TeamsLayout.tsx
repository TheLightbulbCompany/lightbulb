"use client";

import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import { CommandPaletteProvider } from "./command-palette/CommandPaletteProvider";
import { TeamsEventsProvider } from "./TeamsEventsProvider";
import { TeamsSidebar } from "./TeamsSidebar";

export function TeamsLayout({ children }: { children: React.ReactNode }) {
  const status = useTeamsWorkspaceStatus();

  return (
    <div className="editorial-page flex h-screen overflow-hidden">
      <TeamsSidebar />
      <main className="flex-1 overflow-auto">
        {status.kind === "provisioning" ? (
          <ProvisioningOverlay />
        ) : status.kind === "subscribe_required" ? (
          <SubscribeOverlay />
        ) : status.kind === "error" ? (
          <ErrorOverlay error={status.error} />
        ) : (
          <TeamsEventsProvider>
            <CommandPaletteProvider>{children}</CommandPaletteProvider>
          </TeamsEventsProvider>
        )}
      </main>
    </div>
  );
}

function OverlayShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center px-6 py-12">
      <div className="editorial-card w-full max-w-xl px-8 py-10">{children}</div>
    </div>
  );
}

function ProvisioningOverlay() {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-[28px] leading-tight text-[var(--ink)]">
        <em className="italic">Setting up</em> your Teams workspace.
      </h1>
      <p className="mt-3 text-sm text-[var(--ink-soft)]">
        This usually takes about 30 seconds. The page will refresh automatically.
      </p>
    </OverlayShell>
  );
}

function SubscribeOverlay() {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-[28px] leading-tight text-[var(--ink)]">
        <em className="italic">Subscribe</em> to enable Teams.
      </h1>
      <p className="mt-3 text-sm text-[var(--ink-soft)]">
        Teams runs on top of your agent container. Start a subscription from the
        chat page first, then come back.
      </p>
    </OverlayShell>
  );
}

function ErrorOverlay({ error }: { error: Error }) {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-[28px] leading-tight text-[var(--ink)]">
        <em className="italic">Something</em> went wrong.
      </h1>
      <p
        className="mt-4 rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-3 py-2 text-sm text-[var(--danger)]"
        role="alert"
      >
        {String(error.message ?? error)}
      </p>
    </OverlayShell>
  );
}
