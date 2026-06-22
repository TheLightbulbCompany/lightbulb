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
import { cn } from "@/lib/utils";
import { Logo } from "@/components/Logo";
import { UsagePanel } from "@/components/control/panels/UsagePanel";
import { LLMPanel } from "@/components/control/panels/LLMPanel";
import { IntegrationsPanel } from "@/components/control/panels/IntegrationsPanel";
import { SkillsTab } from "@/components/control/panels/SkillsTab";
import { SkillProposalsPanel } from "@/components/control/panels/SkillProposalsPanel";
import { DreamsPanel } from "@/components/control/panels/DreamsPanel";
import { ChannelsStatusPanel } from "@/components/control/panels/ChannelsStatusPanel";
import { ADMIN_ONLY_PANELS, useIsControlAdmin } from "@/components/control/ControlSidebar";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";

// =============================================================================
// Types & Navigation
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

// A nav item is either an in-page tab (sets activePanel) or an outbound
// link to a sibling settings route (`href`). Connections lives on its own
// route — /settings/connections — because OAuth providers redirect the
// browser back to it after consent, so it can't be a ?panel= tab.
type NavItem = {
  id: string;
  name: string;
  icon: string;
  panel?: Panel;
  href?: string;
};

// Legacy nav — the single "Account" group that ships today. Rendered when the
// unified-nav flag is OFF so /settings stays byte-identical to the current app:
// config panels stay in the Control tab, NOT relocated here, until the flag
// flips. Keep this in lockstep with origin/main's NAV_SECTIONS.
const LEGACY_NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "Account",
    items: [
      { id: "profile", name: "Profile", icon: "profile", panel: "profile" },
      { id: "billing", name: "Billing", icon: "billing", panel: "billing" },
      { id: "channels", name: "Channels", icon: "channels", panel: "channels" },
      { id: "connections", name: "Connections", icon: "connections", href: "/settings/connections" },
      { id: "webhooks", name: "Webhooks", icon: "webhooks", panel: "webhooks" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
      { id: "marketplace", name: "Marketplace", icon: "marketplace", panel: "marketplace" },
    ],
  },
];

// Six intent-ordered groups (design.md §5). This increment re-buckets the
// EXISTING panels only and stands up the (currently empty) MODEL & PROVIDERS /
// ENGINE ROOM headers; the Control panels that fill them are registered in the
// following increments via the per-panel insertion checklist. Rendered ONLY
// when the unified-nav flag is ON.
const UNIFIED_NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "ACCOUNT",
    items: [
      { id: "profile", name: "Profile", icon: "profile", panel: "profile" },
    ],
  },
  {
    label: "BILLING & USAGE",
    items: [
      { id: "billing", name: "Billing", icon: "billing", panel: "billing" },
      // Admin-gated: hidden from non-admin org members via ADMIN_ONLY_PANELS.
      { id: "usage", name: "Usage", icon: "usage", panel: "usage" },
    ],
  },
  {
    label: "MODEL & PROVIDERS",
    items: [
      // LLMPanel verbatim — it already embeds CreditsSection + ModelRoutingCard,
      // so there is NO separate Credits row.
      { id: "providers", name: "Model & Providers", icon: "providers", panel: "providers" },
    ],
  },
  {
    label: "WIRING",
    items: [
      { id: "channels", name: "Channels", icon: "channels", panel: "channels" },
      { id: "connections", name: "Connections", icon: "connections", href: "/settings/connections" },
      // MCP servers (owner-level mcp.servers) — the IntegrationsPanel tab; the
      // OAuth-return *route* above (Connections) is the dedup counterpart (§5).
      { id: "mcp", name: "MCP servers", icon: "mcp", panel: "mcp" },
      { id: "webhooks", name: "Webhooks", icon: "webhooks", panel: "webhooks" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
    ],
  },
  {
    label: "ENGINE ROOM",
    items: [
      { id: "skills", name: "Skills", icon: "skills", panel: "skills" },
      { id: "skill-proposals", name: "Skill Proposals", icon: "skill-proposals", panel: "skill-proposals" },
      { id: "dreams", name: "Dreams / Memory", icon: "dreams", panel: "dreams" },
      // Read-only channel health. `channel-status` is DISTINCT from the WIRING
      // `channels` org-bots tab — they are two surfaces, no key collision.
      { id: "channel-status", name: "Channel Status", icon: "channel-status", panel: "channel-status" },
    ],
  },
  {
    label: "SELLER",
    items: [
      { id: "marketplace", name: "Marketplace", icon: "marketplace", panel: "marketplace" },
    ],
  },
];

// =============================================================================
// Constants
// =============================================================================

// =============================================================================
// Helpers
// =============================================================================

function formatDollars(amount: number, decimals = 2): string {
  return `$${amount.toFixed(decimals)}`;
}

// =============================================================================
// Icons
// =============================================================================

function NavIcon({ icon }: { icon: string }) {
  switch (icon) {
    case "profile":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
      );
    case "billing":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="1" y="4" width="22" height="16" rx="2" />
          <line x1="1" y1="10" x2="23" y2="10" />
        </svg>
      );
    case "channels":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      );
    case "webhooks":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2" />
          <path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06" />
          <path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8" />
        </svg>
      );
    case "marketplace":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 9l1-5h16l1 5" />
          <path d="M3 9v11a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V9" />
          <path d="M8 13h8" />
        </svg>
      );
    case "connections":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 22v-5" />
          <path d="M9 8V2" />
          <path d="M15 8V2" />
          <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" />
        </svg>
      );
    case "usage":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <line x1="18" y1="20" x2="18" y2="10" />
          <line x1="12" y1="20" x2="12" y2="4" />
          <line x1="6" y1="20" x2="6" y2="14" />
        </svg>
      );
    case "providers":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
      );
    case "mcp":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="7" height="7" />
          <rect x="14" y="3" width="7" height="7" />
          <rect x="14" y="14" width="7" height="7" />
          <rect x="3" y="14" width="7" height="7" />
        </svg>
      );
    case "skills":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
      );
    case "skill-proposals":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <path d="m9 15 2 2 4-4" />
        </svg>
      );
    case "dreams":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z" />
        </svg>
      );
    case "channel-status":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="2" />
          <path d="M4.93 19.07a10 10 0 0 1 0-14.14" />
          <path d="M7.76 16.24a6 6 0 0 1 0-8.49" />
          <path d="M16.24 7.76a6 6 0 0 1 0 8.49" />
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
        </svg>
      );
    default:
      return null;
  }
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
  mcp: IntegrationsPanel,
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

// The config panels relocated under the unified nav. They live in the PANELS
// registry (so the deep-link/render machinery stays uniform), but are reachable
// ONLY when the flag is ON — flag-OFF they stay in the Control tab. A flag-OFF
// `?panel=<one of these>` falls back to the default `profile` panel so config
// never double-mounts (Control tab + Settings), per spec §2/§5.
const UNIFIED_ONLY_PANELS = new Set<Panel>([
  "usage",
  "providers",
  "mcp",
  "skills",
  "skill-proposals",
  "dreams",
  "channel-status",
]);

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
  // The Settings consolidation (6-group nav + relocated config panels) is
  // dev/flag-on ONLY. Flag-OFF renders the legacy single-group nav and only the
  // legacy panels — byte-identical to today's app, config staying in the
  // Control tab.
  const unifiedNav = useUnifiedNav();
  const navSections = unifiedNav ? UNIFIED_NAV_SECTIONS : LEGACY_NAV_SECTIONS;

  // Personal owners are admins; org members are admins only with org:admin.
  // Reused (not re-derived) from ControlSidebar so the Settings gate matches
  // the Control surface exactly.
  const isAdmin = useIsControlAdmin();

  // ?panel=<key> preselects the tab. Used by deep links from the chat banners
  // (TrialBanner "Manage" CTA, etc.) so they land on the right pane. Validated
  // against the live PANELS registry — any registered panel is deep-linkable —
  // and admin-only panels fall back to profile for non-admins so a stray
  // ?panel=usage never mounts an admin RPC for a member. The relocated config
  // panels are reachable ONLY under the flag: flag-OFF a `?panel=<relocated>`
  // falls back to `profile` so config never double-mounts (Control + Settings).
  const searchParams = useSearchParams();
  const initialPanelParam = searchParams.get("panel");
  const initialPanel: Panel =
    initialPanelParam !== null &&
    initialPanelParam in PANELS &&
    (unifiedNav || !UNIFIED_ONLY_PANELS.has(initialPanelParam as Panel)) &&
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

  function handleNavKeyDown(
    e: React.KeyboardEvent,
    item: NavItem & { panel: Panel },
  ) {
    const idx = tabNavItems.findIndex((n) => n.id === item.id);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setActivePanel(item.panel);
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
        <nav className="settings-nav" aria-label="Settings navigation">
          {navSections.map((section) => (
            <div key={section.label}>
              <div className="settings-nav-section-label">{section.label}</div>
              <ul className="settings-nav-items" role="tablist">
                {section.items
                  .filter(
                    (item) =>
                      isAdmin ||
                      item.panel === undefined ||
                      !ADMIN_ONLY_PANELS.has(item.panel),
                  )
                  .map((item) =>
                  item.href ? (
                    // Outbound link to a sibling settings route — rendered
                    // as a plain link, not a tab, so it navigates away.
                    <li key={item.id} role="none">
                      <Link href={item.href} className="settings-nav-item" style={{ color: "inherit", textDecoration: "none" }}>
                        <NavIcon icon={item.icon} />
                        {item.name}
                      </Link>
                    </li>
                  ) : (
                    <li
                      key={item.id}
                      id={`settings-nav-${item.id}`}
                      className={cn("settings-nav-item", activePanel === item.panel && "active")}
                      role="tab"
                      tabIndex={activePanel === item.panel ? 0 : -1}
                      aria-selected={activePanel === item.panel}
                      aria-controls={`panel-${item.panel}`}
                      onClick={() => item.panel && setActivePanel(item.panel)}
                      onKeyDown={(e) => item.panel && handleNavKeyDown(e, item as NavItem & { panel: Panel })}
                    >
                      <NavIcon icon={item.icon} />
                      {item.name}
                    </li>
                  ),
                )}
              </ul>
            </div>
          ))}
        </nav>

        <main className="settings-main">
          <ActivePanelComponent key={activePanel} />
        </main>
      </div>
      </div>
    </>
  );
}

