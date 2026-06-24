"use client";

import "./settings.css";
import React, { Suspense, useEffect, useState, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { useAuth, useUser, UserButton, useOrganization } from "@clerk/nextjs";
import { useBilling } from "@/hooks/useBilling";
import { MyChannelsSection } from "@/components/settings/MyChannelsSection";
import { WebhooksPanel } from "@/components/settings/WebhooksPanel";
import { DesktopFileAccess } from "@/components/desktop/DesktopFileAccess";
import { HandlePicker } from "@/components/marketplace/HandlePicker";
import { getMySellerProfile, type SellerProfile } from "@/lib/marketplace/api";
import { Logo } from "@/components/Logo";
import { UsagePanel } from "@/components/control/panels/UsagePanel";
import { LLMPanel } from "@/components/control/panels/LLMPanel";
import { IntegrationsPanel } from "@/components/control/panels/IntegrationsPanel";
import { SkillsTab } from "@/components/control/panels/SkillsTab";
import { SkillProposalsPanel } from "@/components/control/panels/SkillProposalsPanel";
import { DreamsPanel } from "@/components/control/panels/DreamsPanel";
import { ChannelsStatusPanel } from "@/components/control/panels/ChannelsStatusPanel";
import { ADMIN_ONLY_PANELS, useIsControlAdmin } from "@/components/control/ControlSidebar";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  SETTINGS_NAV_SECTIONS,
  type NavItem,
} from "@/components/settings/settings-nav-config";

// =============================================================================
// Types & Navigation
//
// The nav sections, the NavItem type, and NavIcon live in the shared
// settings-nav-config module so /settings and /settings/connections render
// the identical sidebar from ONE source. This page re-imports the section
// consts + NavItem above; the connections route imports the same module via
// <SettingsNav>.
// =============================================================================

type Panel =
  | "profile"
  | "billing"
  | "usage"
  | "providers"
  | "channels"
  | "mcp"
  | "webhooks"
  | "desktop"
  | "skills"
  | "skill-proposals"
  | "dreams"
  | "channel-status"
  | "marketplace";

// =============================================================================
// Helpers
// =============================================================================

function formatDollars(amount: number, decimals = 2): string {
  return `$${amount.toFixed(decimals)}`;
}

// =============================================================================
// Profile Panel
// =============================================================================

function ProfilePanel() {
  const { user } = useUser();
  const { account } = useBilling();
  const subscriptionStatus = account?.subscription_status ?? null;

  const email = user?.primaryEmailAddress?.emailAddress ?? "";
  const initials = `${(user?.firstName ?? "")[0] ?? ""}${(user?.lastName ?? "")[0] ?? ""}`.toUpperCase();

  return (
    <div className="settings-panel" id="panel-profile" role="tabpanel">
      <h1 className="settings-page-title">Profile</h1>
      <p className="settings-page-desc">Your account information, managed by your authentication provider.</p>

      <div className="settings-card">
        <div className="settings-profile-row">
          <div className="settings-profile-avatar-lg" aria-hidden="true">{initials}</div>
          <div>
            <div className="settings-profile-name">
              {user?.fullName ?? "User"}
              {subscriptionStatus && (
                <span className="settings-profile-badge">{subscriptionStatus}</span>
              )}
            </div>
            <div className="settings-profile-email">{email}</div>
          </div>
        </div>
        <div className="settings-card-desc" style={{ marginTop: 0 }}>
          To update your name, email, or password, use the account menu in the top-right corner of the app.
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Billing Panel
// =============================================================================

function BillingPanel() {
  const {
    account,
    isLoading,
    error: accountError,
    isSubscribed,
    openPortal,
  } = useBilling();
  const { membership } = useOrganization();

  const [portalLoading, setPortalLoading] = useState(false);

  const handlePortal = useCallback(async () => {
    capture("billing_portal_opened");
    setPortalLoading(true);
    try {
      await openPortal();
    } catch {
      setPortalLoading(false);
    }
  }, [openPortal]);

  if (isLoading) {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your subscription.</p>
        <div style={{ display: "flex", justifyContent: "center", padding: "60px 0" }}>
          <div className="settings-spinner" />
        </div>
      </div>
    );
  }

  if (membership && membership.role !== "org:admin") {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <div className="settings-restricted">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#59534d" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
          <div className="settings-restricted-title">Billing restricted</div>
          <div className="settings-restricted-desc">Contact your organization admin to manage billing and subscription settings.</div>
        </div>
      </div>
    );
  }

  const subscriptionStatus = account?.subscription_status ?? null;

  return (
    <div className="settings-panel" id="panel-billing" role="tabpanel">
      <h1 className="settings-page-title">Billing</h1>
      <p className="settings-page-desc">Manage your subscription.</p>

      {accountError && (
        <div className="settings-alert settings-alert-error">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          Failed to load billing data. Please refresh the page.
        </div>
      )}

      <div className="settings-card">
        <div className="settings-card-header">
          <div>
            <div className="settings-card-title">
              Isol8 — $50 / month
              {subscriptionStatus && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 8, fontSize: 11, fontWeight: 600, color: "#06402B", background: "rgba(6,64,43,.08)", padding: "2px 8px", borderRadius: 999, textTransform: "capitalize" }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  {subscriptionStatus}
                </span>
              )}
            </div>
            <div className="settings-card-desc">
              {subscriptionStatus === "trialing"
                ? "Free trial — Stripe will email you before it converts."
                : isSubscribed
                  ? "Active subscription"
                  : "No active subscription"}
            </div>
            {account && (
              <div className="settings-card-desc" style={{ marginTop: 4 }}>
                Current period spend: {formatDollars(account.current_spend, 4)} · Lifetime: {formatDollars(account.lifetime_spend)}
              </div>
            )}
          </div>
          {isSubscribed ? (
            <button type="button" className="settings-btn-outline" onClick={handlePortal} disabled={portalLoading}>
              {portalLoading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Manage Payment
            </button>
          ) : (
            <Link href="/chat" className="settings-btn-save">
              Sign up
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Channels Panel
// =============================================================================

function ChannelsPanel() {
  return (
    <div className="settings-panel" id="panel-channels" role="tabpanel">
      <h1 className="settings-page-title">Channels</h1>
      <p className="settings-page-desc">Link your personal Telegram, Discord, and Slack accounts to your organization&apos;s bots.</p>
      <MyChannelsSection />
    </div>
  );
}

// =============================================================================
// Marketplace Panel (seller handle + display name)
// =============================================================================

function MarketplacePanel() {
  return (
    <div className="settings-panel" id="panel-marketplace" role="tabpanel">
      <h1 className="settings-page-title">Marketplace</h1>
      <p className="settings-page-desc">
        Your public identity on the marketplace. The handle appears as
        <span className="font-mono">{" "}@you{" "}</span>
        on every listing byline and on (future) star / credibility surfaces.
      </p>
      <MarketplaceProfileEditor />
    </div>
  );
}

function MarketplaceProfileEditor() {
  const { getToken, isLoaded } = useAuth();
  const [profile, setProfile] = useState<SellerProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    if (!isLoaded) return;
    let cancelled = false;
    (async () => {
      try {
        const jwt = await getToken();
        if (!jwt) return;
        const fetched = await getMySellerProfile({ jwt });
        if (!cancelled) setProfile(fetched);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isLoaded, getToken]);

  if (loading) {
    return <div className="text-[13px] text-[#8a8478]">Loading your profile…</div>;
  }

  return (
    <HandlePicker
      initialProfile={profile}
      saveLabel={profile ? "Update profile" : "Save profile"}
      onSaved={(next) => {
        setProfile(next);
        setSavedAt(Date.now());
      }}
    />
  );
  // savedAt isn't rendered to keep the section minimal — the input border /
  // availability indicator is feedback enough, and the persisted profile is
  // visible in the form fields the moment the request resolves.
  void savedAt;
}

// =============================================================================
// Panel Registry
// =============================================================================

const PANELS: Record<Panel, () => React.ReactElement> = {
  profile: ProfilePanel,
  billing: BillingPanel,
  usage: UsagePanel,
  providers: LLMPanel,
  channels: ChannelsPanel,
  // Settings variant: renders inside the shared `.settings-panel` frame so MCP
  // servers reads as the same surface as the other Settings panels (the Control
  // tab keeps the default full-bleed variant via ControlPanelRouter).
  mcp: () => <IntegrationsPanel variant="settings" />,
  webhooks: WebhooksPanel,
  desktop: DesktopFileAccess,
  // SkillsTab takes an optional `agentId`; mounting it prop-less yields the
  // owner-level (global) skills surface — the engine-room home.
  skills: () => <SkillsTab />,
  "skill-proposals": SkillProposalsPanel,
  dreams: DreamsPanel,
  "channel-status": ChannelsStatusPanel,
  marketplace: MarketplacePanel,
};

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
  // The Settings consolidation: 6-group nav + the config panels relocated from
  // the Control tab (Usage, Model & Providers, MCP servers, the ENGINE ROOM
  // trio, Channel Status).
  const navSections = SETTINGS_NAV_SECTIONS;

  // Personal owners are admins; org members are admins only with org:admin.
  // Reused (not re-derived) from ControlSidebar so the Settings gate matches
  // the Control surface exactly.
  const isAdmin = useIsControlAdmin();

  // ?panel=<key> preselects the tab. Used by deep links from the chat banners
  // (TrialBanner "Manage" CTA, etc.) so they land on the right pane. Validated
  // against the live PANELS registry — any registered panel is deep-linkable —
  // and admin-only panels fall back to profile for non-admins so a stray
  // ?panel=usage never mounts an admin RPC for a member.
  const searchParams = useSearchParams();
  const initialPanelParam = searchParams.get("panel");
  const initialPanel: Panel =
    initialPanelParam !== null &&
    initialPanelParam in PANELS &&
    (isAdmin || !ADMIN_ONLY_PANELS.has(initialPanelParam))
      ? (initialPanelParam as Panel)
      : "profile";
  const [activePanel, setActivePanel] = useState<Panel>(initialPanel);
  const ActivePanelComponent = PANELS[activePanel];

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
    const idx = tabNavItems.findIndex((n) => n.id === item.id);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setActivePanel(item.panel as Panel);
    }
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = tabNavItems[(idx + 1) % tabNavItems.length];
      setActivePanel(next.panel);
      document.getElementById(`settings-nav-${next.id}`)?.focus();
    }
    if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = tabNavItems[(idx - 1 + tabNavItems.length) % tabNavItems.length];
      setActivePanel(prev.panel);
      document.getElementById(`settings-nav-${prev.id}`)?.focus();
    }
  }

  return (
    <>

      {/* Top bar */}
      <header className="settings-topbar" style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}>
        <div className="settings-topbar-left">
          <Logo size="md" />
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
      <div className="settings-page-wrap" style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}>
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

