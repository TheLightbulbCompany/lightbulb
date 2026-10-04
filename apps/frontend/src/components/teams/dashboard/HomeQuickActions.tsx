"use client";

// The empty state's real payload. When the Business section has nothing to
// report, this row is what the owner can DO about it. Only verbs that resolve
// to a surface that exists today; a prompt the agent cannot act on is worse
// than no prompt (PanelNotice.tsx, `prompt`). Content / Publish / Write
// article arrive with their tabs, not before.

import type { MouseEvent } from "react";
import { MessageSquare, Plus, UserPlus, Package } from "lucide-react";
import { useAgents } from "@/hooks/useAgents";
import { openChatPanel } from "@/components/workspace/workspaceController";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";

const CHIP =
  "inline-flex items-center gap-1.5 rounded-md border border-[var(--edge)] bg-card px-3 py-2 label-caps text-[var(--midnight)] no-underline transition-colors hover:bg-[var(--cream-deep)]";

const NEW_TASK_URL = `${teamsPanelUrl("issues")}&new=task`;
// The company account is a full-Dashboard Stripe account the owner logs into
// (core/services/company_stripe.py). Provider-native, like Result's "OPEN …".
const STRIPE_NEW_PRODUCT_URL = "https://dashboard.stripe.com/products/create";

function isPlainClick(e: MouseEvent): boolean {
  return !(
    e.defaultPrevented ||
    e.metaKey ||
    e.ctrlKey ||
    e.shiftKey ||
    e.altKey ||
    e.button !== 0
  );
}

export function HomeQuickActions({ stripeBound }: { stripeBound: boolean }) {
  const { agents, defaultId, isSettled } = useAgents();
  const defaultAgent = agents.find((agent) => agent.id === defaultId);
  const ceoName = defaultAgent?.name || defaultAgent?.identity?.name;
  const askLabel = ceoName ? `Ask ${ceoName}` : isSettled ? null : "Ask CEO";
  const { open: openMarketplace } = useMarketplaceDrawer();

  // Mirrors WorkspaceShell's Get Started "chat" action: record the focus
  // intent before opening; delivery waits for the selected room.
  const askCeo = (e: MouseEvent<HTMLAnchorElement>) => {
    // Modifier / middle click: leave it alone, the href opens a real tab.
    if (!isPlainClick(e)) return;
    // No default agent yet while the roster is still loading. The
    // href="/chat" would then HARD-navigate — a full SPA reload of the page the
    // owner is already on, losing the dock layout — so a plain click here does
    // nothing rather than something destructive.
    if (!defaultId) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    requestFocusComposer(defaultId);
    openChatPanel(defaultId);
    window.dispatchEvent(
      new CustomEvent("getstarted:focus-composer", { detail: { agentId: defaultId } }),
    );
  };

  return (
    <section aria-label="Quick actions" className="flex flex-col gap-2">
      <p className="label-caps text-[var(--midnight-muted)]">Quick actions</p>
      <div className="flex flex-wrap gap-2">
        {askLabel && (<>
          {/* A real anchor, like its siblings: the href is what native new-tab /
              middle-click follows, and next/link would only prefetch the route
              the shell is already on. */}
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
          <a href="/chat" onClick={askCeo} className={CHIP}>
            <MessageSquare size={14} strokeWidth={1.75} aria-hidden="true" /> {askLabel}
          </a>
        </>)}
        <a
          href={NEW_TASK_URL}
          onClick={(e) => handleShellLinkClick(e, NEW_TASK_URL)}
          className={CHIP}
        >
          <Plus size={14} strokeWidth={1.75} aria-hidden="true" /> New task
        </a>
        <button type="button" onClick={() => openMarketplace({ view: "browse" })} className={CHIP}>
          <UserPlus size={14} strokeWidth={1.75} aria-hidden="true" /> Hire an agent
        </button>
        {stripeBound && (
          <a
            href={STRIPE_NEW_PRODUCT_URL}
            target="_blank"
            rel="noopener noreferrer"
            className={CHIP}
          >
            <Package size={14} strokeWidth={1.75} aria-hidden="true" /> Add a product
          </a>
        )}
      </div>
    </section>
  );
}
