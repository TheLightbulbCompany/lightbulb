"use client";

import { Loader2 } from "lucide-react";

// Workspace-status overlays for the inline Teams surface. The Teams panels
// render inside the /chat shell (InlineTeamsPanel), which gates on
// useTeamsWorkspaceStatus and shows one of these full-bleed overlays for the
// provisioning / subscribe-required / error states instead of letting the
// panels fall through to a generic loading/error.

function OverlayShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center px-6 py-12">
      <div className="editorial-card w-full max-w-xl px-8 py-10">{children}</div>
    </div>
  );
}

export function ProvisioningOverlay() {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-2xl leading-tight text-[var(--midnight)]">
        <em className="italic">Setting up</em> your Teams workspace.
      </h1>
      {/* The spinner rides BESIDE the copy, never alone: this overlay is what a
          Teams panel shows for the whole provisioning window, and a lone
          spinner there says nothing about what is being waited on. */}
      <p
        className="mt-3 flex items-center gap-2 text-sm text-[var(--midnight-soft)]"
        role="status"
        aria-live="polite"
      >
        <Loader2 size={16} className="shrink-0 animate-spin" aria-hidden="true" />
        <span>This usually takes about 30 seconds. The page will refresh automatically.</span>
      </p>
    </OverlayShell>
  );
}

export function SubscribeOverlay() {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-2xl leading-tight text-[var(--midnight)]">
        <em className="italic">Subscribe</em> to enable Teams.
      </h1>
      <p className="mt-3 text-sm text-[var(--midnight-soft)]">
        Teams runs on top of your agent container. Start a subscription from the
        chat page first, then come back.
      </p>
    </OverlayShell>
  );
}

export function ErrorOverlay({ error }: { error: Error }) {
  return (
    <OverlayShell>
      <h1 className="font-editorial-serif text-2xl leading-tight text-[var(--midnight)]">
        <em className="italic">Something</em> went wrong.
      </h1>
      <p
        className="mt-4 rounded-lg border border-err bg-cream px-3 py-2 text-ui text-err"
        role="alert"
      >
        {String(error.message ?? error)}
      </p>
    </OverlayShell>
  );
}
