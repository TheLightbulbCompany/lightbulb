"use client";

import "./settings.css";
import React, { Suspense, useState, useCallback, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { useUser, UserButton, useOrganization } from "@clerk/nextjs";
import { useBilling } from "@/hooks/useBilling";
import { MyChannelsSection } from "@/components/settings/MyChannelsSection";
import { WebhooksPanel } from "@/components/settings/WebhooksPanel";
import { useApi, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

// =============================================================================
// Types & Navigation
// =============================================================================

type Panel = "profile" | "billing" | "channels" | "webhooks" | "seller";

const NAV_SECTIONS = [
  {
    label: "Account",
    items: [
      { id: "profile" as Panel, name: "Profile", icon: "profile" },
      { id: "billing" as Panel, name: "Billing", icon: "billing" },
      { id: "channels" as Panel, name: "Channels", icon: "channels" },
      { id: "webhooks" as Panel, name: "Webhooks", icon: "webhooks" },
      { id: "seller" as Panel, name: "Seller handle", icon: "profile" },
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
// Seller Panel — claim + change marketplace handle
// =============================================================================

const USERNAME_REGEX = /^[a-z0-9_-]+$/;
const USERNAME_MIN = 3;
const USERNAME_MAX = 30;

interface SellerState {
  username: string;
  last_changed_at: string | null;
  cooldown_ends_at: string | null;
}

function SellerPanel() {
  const api = useApi();
  const [state, setState] = useState<SellerState | null>(null);
  const [loading, setLoading] = useState(true);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [okMessage, setOkMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = (await api.get("/sellers/me")) as SellerState;
        if (!cancelled) {
          setState(s);
          setInput(s.username);
        }
      } catch (err) {
        const e = err as ApiError;
        // 404 = not yet claimed; keep state=null so the UI renders the
        // claim form rather than the change form.
        if (e?.status !== 404 && !cancelled) {
          setError(e?.message || "Failed to load handle");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const trimmed = input.trim().toLowerCase();
  const formatOk =
    trimmed.length >= USERNAME_MIN &&
    trimmed.length <= USERNAME_MAX &&
    USERNAME_REGEX.test(trimmed);

  const cooldownActive = (() => {
    if (!state?.cooldown_ends_at) return false;
    return new Date(state.cooldown_ends_at).getTime() > Date.now();
  })();

  async function submit() {
    setSubmitting(true);
    setError(null);
    setOkMessage(null);
    try {
      const next = (await api.post("/sellers/me/username", {
        username: trimmed,
      })) as SellerState;
      setState(next);
      setInput(next.username);
      setOkMessage(`Handle set to @${next.username}.`);
    } catch (err) {
      const e = err as ApiError;
      const body = e?.body as { detail?: { error?: string; cooldown_ends_at?: string } } | null;
      const code = body?.detail?.error ?? "";
      if (code === "taken") {
        setError("That handle is taken.");
      } else if (code === "reserved") {
        setError("That handle is reserved.");
      } else if (code === "invalid_format") {
        setError("Use 3-30 lowercase letters, digits, hyphens, or underscores.");
      } else if (code === "cooldown_active") {
        const next = body?.detail?.cooldown_ends_at;
        setError(
          next
            ? `You can change your handle again on ${new Date(next).toLocaleDateString()}.`
            : "You can only change your handle every 90 days.",
        );
      } else {
        setError(e?.message || "Failed to update handle");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="settings-panel" id="panel-seller" role="tabpanel">
      <h1 className="settings-page-title">Seller handle</h1>
      <p className="settings-page-desc">
        Public marketplace handle. Buyers see <strong>by @{state?.username ?? "yourhandle"}</strong>{" "}
        on every listing you publish. Handles are unique and can be changed once every 90 days.
      </p>

      <div className="settings-card">
        <div className="settings-card-desc" style={{ marginBottom: 12 }}>
          {loading
            ? "Loading…"
            : state
              ? `Current handle: @${state.username}`
              : "Not set — claim one before publishing your first listing."}
        </div>

        {cooldownActive && state?.cooldown_ends_at && (
          <div className="settings-alert" style={{ marginBottom: 12 }}>
            You can change this handle again on{" "}
            {new Date(state.cooldown_ends_at).toLocaleDateString()}.
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <span style={{ color: "#5b5b5b" }}>@</span>
          <input
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              setError(null);
              setOkMessage(null);
            }}
            placeholder="ledger"
            maxLength={USERNAME_MAX}
            disabled={cooldownActive || submitting}
            className="settings-input"
            style={{
              flex: 1,
              padding: "8px 12px",
              border: "1px solid #e0dbd0",
              borderRadius: 10,
              fontSize: 14,
            }}
          />
          <button
            type="button"
            onClick={() => void submit()}
            disabled={
              !formatOk ||
              submitting ||
              cooldownActive ||
              trimmed === state?.username
            }
            className="settings-btn-save"
          >
            {state ? "Change" : "Claim"}
          </button>
        </div>
        <div className="settings-card-desc">
          3-30 chars · lowercase letters, digits, hyphen, underscore.
        </div>

        {error && (
          <p role="alert" className="settings-alert settings-alert-error" style={{ marginTop: 12 }}>
            {error}
          </p>
        )}
        {okMessage && (
          <p className="settings-alert" style={{ marginTop: 12 }}>
            {okMessage}
          </p>
        )}
      </div>
    </div>
  );
}

// =============================================================================
// Panel Registry
// =============================================================================

const PANELS: Record<Panel, () => React.ReactElement> = {
  profile: ProfilePanel,
  billing: BillingPanel,
  channels: ChannelsPanel,
  webhooks: WebhooksPanel,
  seller: SellerPanel,
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
  // ?panel=billing|profile|channels preselects the tab. Used by deep
  // links from the chat banners (TrialBanner "Manage" CTA, etc.) so they
  // land on the right pane. Codex P2 on PR #393 — the prior /settings/
  // billing href was a 404.
  const searchParams = useSearchParams();
  const initialPanelParam = searchParams.get("panel");
  const initialPanel: Panel =
    initialPanelParam === "billing" ||
    initialPanelParam === "channels" ||
    initialPanelParam === "webhooks" ||
    initialPanelParam === "seller"
      ? initialPanelParam
      : "profile";
  const [activePanel, setActivePanel] = useState<Panel>(initialPanel);
  const ActivePanelComponent = PANELS[activePanel];

  const allNavItems = NAV_SECTIONS.flatMap((s) => s.items);

  function handleNavKeyDown(e: React.KeyboardEvent, item: (typeof allNavItems)[number]) {
    const idx = allNavItems.findIndex((n) => n.id === item.id);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setActivePanel(item.id);
    }
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = allNavItems[(idx + 1) % allNavItems.length];
      setActivePanel(next.id);
      document.getElementById(`settings-nav-${next.id}`)?.focus();
    }
    if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = allNavItems[(idx - 1 + allNavItems.length) % allNavItems.length];
      setActivePanel(prev.id);
      document.getElementById(`settings-nav-${prev.id}`)?.focus();
    }
  }

  return (
    <>

      {/* Top bar */}
      <header className="settings-topbar" style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}>
        <div className="settings-topbar-left">
          <Link href="/" className="settings-topbar-logo" aria-label="isol8 home">
            <svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect width="100" height="100" rx="22" fill="#06402B" />
              <text x="50" y="68" textAnchor="middle" fontFamily="'Lora', serif" fontStyle="italic" fontSize="52" fill="white">8</text>
            </svg>
          </Link>
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
          {NAV_SECTIONS.map((section) => (
            <div key={section.label}>
              <div className="settings-nav-section-label">{section.label}</div>
              <ul className="settings-nav-items" role="tablist">
                {section.items.map((item) => (
                  <li
                    key={item.id}
                    id={`settings-nav-${item.id}`}
                    className={cn("settings-nav-item", activePanel === item.id && "active")}
                    role="tab"
                    tabIndex={activePanel === item.id ? 0 : -1}
                    aria-selected={activePanel === item.id}
                    aria-controls={`panel-${item.id}`}
                    onClick={() => setActivePanel(item.id)}
                    onKeyDown={(e) => handleNavKeyDown(e, item)}
                  >
                    <NavIcon icon={item.icon} />
                    {item.name}
                  </li>
                ))}
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

