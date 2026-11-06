"use client";

import "./settings.css";
import React, { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";

import { UserButton } from "@clerk/nextjs";
import { Logo } from "@/components/Logo";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { SettingsNav } from "@/components/settings/SettingsNav";
import { navKeyDown, PANELS, type Panel } from "@/components/settings/SettingsPlace";
import {
  ADMIN_ONLY_PANELS,
  SETTINGS_NAV_SECTIONS,
  type NavItem,
} from "@/components/settings/settings-nav-config";

// =============================================================================
// Settings Page
// =============================================================================

export default function SettingsPage() {
  // useSearchParams() forces dynamic rendering; wrap the inner component
  // in a Suspense boundary so the static-export step at build time
  // doesn't error (Next.js 16 requirement for static prerender).
  return (
    <Suspense fallback={null}>
      <SettingsPageInner />
    </Suspense>
  );
}

function SettingsPageInner() {
  // Super-agent (UX slice 4): Settings lives inside the shell. Old /settings URLs and bookmarks open it there, with
  // `?panel=` as its section and every other param kept (OAuth/Stripe returns, ?action=).
  return useSuperAgent() ? <ToShellSettings /> : <LegacySettingsPage />;
}

function ToShellSettings() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    const next = new URLSearchParams(searchParams.toString());
    const section = next.get("panel");
    next.set("panel", "settings");
    next.delete("section");
    if (section) next.set("section", section);
    router.replace(`/chat?${next.toString()}`);
  }, [router, searchParams]);
  return null;
}

function LegacySettingsPage() {
  // The one Settings page: Control's panels, Team Settings and Accounts, in six
  // groups (settings-nav-config.tsx).
  const navSections = SETTINGS_NAV_SECTIONS;

  // Personal owners are admins; org members are admins only with org:admin.
  const isAdmin = useIsWorkspaceAdmin();

  // ?panel=<key> preselects the tab. Used by deep links from the chat banners
  // (TrialBanner "Manage" CTA, etc.) so they land on the right pane. Validated
  // against the live PANELS registry — any registered panel is deep-linkable —
  // and admin-only panels fall back to profile for non-admins so a stray
  // ?panel=usage never mounts an admin RPC for a member.
  const searchParams = useSearchParams();
  const initialPanelParam = searchParams.get("panel");
  const initialPanel: Panel =
    initialPanelParam !== null &&
    // hasOwnProperty, not `in` — `in` walks the prototype chain, so
    // ?panel=constructor would otherwise pass validation and read
    // Object.prototype.constructor out of PANELS (panelRegistry.ts's
    // hasOwnKey guards the identical case for the workspace's own registry).
    Object.prototype.hasOwnProperty.call(PANELS, initialPanelParam) &&
    (isAdmin || !ADMIN_ONLY_PANELS.has(initialPanelParam))
      ? (initialPanelParam as Panel)
      : "profile";
  // Derived, not latched: isAdmin reads false until Clerk knows the membership,
  // so the deep-linked tab re-resolves once it does. A click wins from then on.
  const [chosenPanel, setActivePanel] = useState<Panel | null>(null);
  // A later ?panel= change (an in-page link, e.g. Skills → Connections) wins
  // over an earlier click: drop the click when the param moves.
  const [seenParam, setSeenParam] = useState(initialPanelParam);
  if (seenParam !== initialPanelParam) {
    setSeenParam(initialPanelParam);
    setActivePanel(null);
  }
  const activePanel = chosenPanel ?? initialPanel;
  const ActivePanelComponent = PANELS[activePanel];

  // …but a `?panel=` value can also name an outbound nav row (`href`), which
  // names nothing in PANELS and would fall back to Profile silently. Forward it.
  const router = useRouter();
  const outboundHref = SETTINGS_NAV_SECTIONS.flatMap((s) => s.items).find(
    (n) => n.href !== undefined && n.id === initialPanelParam,
  )?.href;
  useEffect(() => {
    if (!outboundHref) return;
    // Forward the rest of the query too (?checkout=, ?connected=, ?action= …): the
    // destination's return-banner and one-shot handlers read them.
    const rest = new URLSearchParams(searchParams.toString());
    rest.delete("panel");
    const [path, query = ""] = outboundHref.split("?");
    const merged = new URLSearchParams(query);
    rest.forEach((v, k) => { if (!merged.has(k)) merged.set(k, v); });
    router.replace(`${path}?${merged.toString()}`);
  }, [outboundHref, router, searchParams]);

  // Arrow-key roving focus operates only over the in-page tabs. Outbound
  // links (e.g. Connections) are excluded so they don't try to set a panel
  // that doesn't exist, and admin-only tabs hidden from non-admins are excluded
  // so roving focus never lands on an unrendered row.
  const tabNavItems = navSections.flatMap((s) => s.items).filter(
    (n): n is NavItem & { panel: Panel } =>
      n.panel !== undefined && (isAdmin || !ADMIN_ONLY_PANELS.has(n.panel)),
  );

  // Accepts the shared NavItem & { panel: string } so it matches SettingsNav's
  // onPanelKeyDown prop; the panel keys are constrained to the Panel union by
  // the section consts, so the cast is sound.
  function handleNavKeyDown(
    e: React.KeyboardEvent,
    item: NavItem & { panel: string },
  ) {
    navKeyDown(e, item, tabNavItems, (panel) => setActivePanel(panel as Panel));
  }

  // Redirecting (see outboundHref above) — render nothing rather than flashing
  // the Profile pane on the way out. No hooks run below this line.
  if (outboundHref) return null;

  return (
    <>

      {/* Top bar */}
      <header className="settings-topbar" style={{ fontFamily: "var(--font-sans)" }}>
        <div className="settings-topbar-left">
          <Logo size="md" href="/chat" />
          <nav className="settings-topbar-breadcrumb" aria-label="Breadcrumb">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
            <span className="settings-current" aria-current="page">Settings</span>
          </nav>
        </div>
        <div className="settings-topbar-right">
          <Link href="/chat" className="settings-btn-back-chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M19 12H5" />
              <polyline points="12 19 5 12 12 5" />
            </svg>
            Back to Chat
          </Link>
          <UserButton afterSignOutUrl="/" />
        </div>
      </header>

      {/* Main layout */}
      <div className="settings-page-wrap" style={{ fontFamily: "var(--font-sans)" }}>
      <div className="settings-layout">
        <SettingsNav
          mode="tabs"
          sections={navSections}
          isAdmin={isAdmin}
          activePanel={activePanel}
          onSelectPanel={(panel) => setActivePanel(panel as Panel)}
          onPanelKeyDown={handleNavKeyDown}
        />

        <main className="settings-main">
          <ActivePanelComponent key={activePanel} />
        </main>
      </div>
      </div>
    </>
  );
}
