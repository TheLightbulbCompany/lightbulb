"use client";

import "./settings.css";
import React, { Suspense, useEffect, useState, useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { freeTierDaysLeft, planLabel } from "@/lib/planLabel";
import { useAuth, useUser, UserButton, useOrganization, useClerk } from "@clerk/nextjs";
import { useApi, ApiError, BACKEND_URL } from "@/lib/api";
import { useBilling } from "@/hooks/useBilling";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { WorkspaceMembersSection } from "@/components/settings/WorkspaceMembersSection";
import { DailySummaryCard } from "@/components/settings/DailySummaryCard";
import { SessionNotificationsCard } from "@/components/settings/SessionNotificationsCard";
import { DesktopSettings } from "@/components/desktop/DesktopSettings";
import { LocalMcpServers } from "@/components/desktop/LocalMcpServers";
import { HandlePicker } from "@/components/marketplace/HandlePicker";
import { getMySellerProfile, type SellerProfile } from "@/lib/marketplace/api";
import { Logo } from "@/components/Logo";
import { PlanChooser } from "@/components/settings/PlanChooser";
import { UsagePanel } from "@/components/control/panels/UsagePanel";
import { BudgetsPanel } from "@/components/control/panels/BudgetsPanel";
import { LLMPanel } from "@/components/control/panels/LLMPanel";
import { IntegrationsPanel } from "@/components/control/panels/IntegrationsPanel";
import { SkillsTab } from "@/components/control/panels/SkillsTab";
import { ConnectionsPanel } from "@/components/control/panels/ConnectionsPanel";
import { SettingsPanel as CompanySettingsPanel } from "@/components/teams/panels/SettingsPanel";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  ADMIN_ONLY_PANELS,
  SETTINGS_NAV_SECTIONS,
  type NavItem,
} from "@/components/settings/settings-nav-config";

// =============================================================================
// Types & Navigation
//
// The nav sections, the NavItem type, and NavIcon live in the shared
// settings-nav-config module. This page re-imports the section consts +
// NavItem above; <SettingsNav> imports the same module.
// =============================================================================

type Panel =
  | "profile"
  | "workspace"
  | "members"
  | "connections"
  | "billing"
  | "usage"
  | "budgets"
  | "providers"
  | "mcp"
  | "desktop"
  | "local-mcp"
  | "skills"
  | "marketplace";

// =============================================================================
// Profile Panel
// =============================================================================

const DANGER = "var(--err)";

// Self-serve account deletion (mirrors the mobile settings flow): explain
// permanence, require the account email typed back (same guard the backend
// enforces), run the server-side teardown, then sign out. Every account is a
// workspace member: with others in the workspace only the caller leaves; the
// sole member takes the workspace with them (`POST /users/me/delete`). A
// refusal (e.g. the last admin) comes back as a readable 409 detail.
function DeleteAccountCard() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const api = useApi();

  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const email = user?.primaryEmailAddress?.emailAddress ?? "";

  const handleDelete = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post("/users/me/delete", { confirm_email: typed.trim() });
      // The Clerk user is already gone server-side (last teardown step);
      // signOut just clears the local session. If it throws, hard-redirect.
      await signOut({ redirectUrl: "/" }).catch(() => {
        window.location.href = "/";
      });
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as { detail?: unknown })?.detail === "string"
          ? (err.body as { detail: string }).detail
          : null;
      setError(
        detail === "email_mismatch"
          ? "That email didn't match this account."
          : detail === "deletion_incomplete"
            ? `Deletion didn't finish — try again in a moment, or contact ${SUPPORT_EMAIL}.`
            : (detail ?? `Something went wrong — try again or contact ${SUPPORT_EMAIL}.`),
      );
      setBusy(false);
    }
  }, [api, typed, signOut]);

  return (
    <div className="settings-card" style={{ borderColor: "color-mix(in srgb, var(--err) 25%, transparent)" }}>
      <div className="settings-card-title" style={{ color: DANGER }}>Delete account</div>
      {!open ? (
        <>
          <div className="settings-card-desc">
            Permanently delete your account. If you&apos;re the only member of this workspace, its agents and data
            are deleted too; otherwise only you leave and the workspace keeps running. This cannot be undone.
          </div>
          <button
            type="button"
            className="settings-btn-outline"
            style={{ color: DANGER, borderColor: "color-mix(in srgb, var(--err) 40%, transparent)" }}
            onClick={() => setOpen(true)}
          >
            Delete account…
          </button>
        </>
      ) : (
        <>
          <div className="settings-card-desc">
            Type your account email ({email}) to confirm. This permanently deletes your account — it
            cannot be undone.
          </div>
          <input
            type="email"
            className="settings-overage-input"
            style={{ width: "100%", maxWidth: 360, marginBottom: 10 }}
            placeholder={email}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            disabled={busy}
            aria-label="Confirm account email"
          />
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              className="settings-btn-outline"
              style={{ color: "var(--cream)", background: DANGER, borderColor: DANGER }}
              onClick={handleDelete}
              disabled={busy || typed.trim().toLowerCase() !== email.trim().toLowerCase()}
            >
              {busy && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Delete forever
            </button>
            <button
              type="button"
              className="settings-btn-outline"
              onClick={() => { setOpen(false); setTyped(""); setError(null); }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
          {error && (
            <div className="settings-alert settings-alert-error" style={{ marginTop: 10 }}>
              {error}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function WorkspaceExportCard() {
  const isWorkspaceAdmin = useIsWorkspaceAdmin();
  const { getToken } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleExport = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      const response = await fetch(`${BACKEND_URL}/teams/workspace/export`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      // 202 means the workspace is still provisioning and the body is JSON, not
      // a ZIP; only a 200 carrying a ZIP is a finished export.
      if (response.status === 202) {
        setError("Your workspace is still setting up. Try again in a minute.");
        return;
      }
      const contentType = response.headers.get("content-type") ?? "";
      if (response.status !== 200 || !contentType.includes("application/zip")) {
        throw new Error(`Export failed (${response.status})`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") ?? "";
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? "lightbulb-workspace.zip";
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      capture("workspace_export_downloaded");
    } catch {
      setError(`We couldn't prepare your export. Try again, or contact ${SUPPORT_EMAIL}.`);
    } finally {
      setBusy(false);
    }
  }, [getToken]);

  if (!isWorkspaceAdmin) return null;
  return (
    <div className="settings-card" data-testid="workspace-export-card">
      <div className="settings-card-title">Export workspace</div>
      <div className="settings-card-desc">
        Download a ZIP containing this workspace&apos;s room transcripts and board tasks. This stays available
        here when the workspace is read-only, so you can keep your team&apos;s record.
      </div>
      <button type="button" className="settings-btn-outline" onClick={handleExport} disabled={busy}>
        {busy ? "Preparing export…" : "Download workspace ZIP"}
      </button>
      {error && <div className="settings-alert settings-alert-error" style={{ marginTop: 10 }}>{error}</div>}
    </div>
  );
}

function ProfilePanel() {
  const { user } = useUser();
  const { account } = useBilling();
  const subscriptionStatus = account?.subscription_status ?? null;
  const isTrialLocal = subscriptionStatus === "trial_local";

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
              {planLabel(subscriptionStatus) && (
                // .settings-profile-badge is text-transform: uppercase
                // (settings.css) — fine for "ACTIVE" but would shout "FREE
                // TIER". Override just the transform for the mapped label,
                // same technique as the billing chip below.
                <span
                  className="settings-profile-badge"
                  style={{ textTransform: isTrialLocal ? "none" : undefined }}
                >
                  {planLabel(subscriptionStatus)}
                </span>
              )}
            </div>
            <div className="settings-profile-email">{email}</div>
          </div>
        </div>
        <div className="settings-card-desc" style={{ marginTop: 0 }}>
          To update your name, email, or password, use the account menu in the top-right corner of the app.
        </div>
      </div>

      <DailySummaryCard />

      <SessionNotificationsCard />

      <WorkspaceExportCard />

      <DeleteAccountCard />
    </div>
  );
}

// =============================================================================
// Members Panel
// =============================================================================

function MembersPanel() {
  return (
    <div className="settings-panel" id="panel-members" role="tabpanel">
      <h1 className="settings-page-title">Members</h1>
      <p className="settings-page-desc">
        Who&apos;s in this workspace, and the link that lets someone else in. Everyone
        shares the same agents, rooms, and subscription. The plan covers five people; each
        member from the 6th adds one seat to the workspace&apos;s bill, prorated from the day
        they join.
      </p>
      <WorkspaceMembersSection />
    </div>
  );
}

// =============================================================================
// Workspace Panel (the company's name and description, from Team Settings)
// =============================================================================

function WorkspacePanel() {
  return (
    <div className="settings-panel" id="panel-workspace" role="tabpanel">
      <h1 className="settings-page-title">Workspace</h1>
      <p className="settings-page-desc">The company&apos;s name and description, as your agents see it.</p>
      <CompanySettingsPanel />
    </div>
  );
}

// =============================================================================
// Model Panel — `?action=change-provider` (the provision gate, ChatErrorCard)
// opens the provider picker once, then leaves the URL.
// =============================================================================

function ModelPanel() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const raw = searchParams.get("action");
  // "connect-chatgpt": Checkout's return for a free workspace that picked ChatGPT (routers/billing.py `then`).
  const action = raw === "change-provider" || raw === "connect-chatgpt" ? raw : null;
  return (
    <LLMPanel
      initialAction={action}
      onActionConsumed={() => {
        const rest = new URLSearchParams(searchParams.toString());
        rest.delete("action");
        router.replace(`${pathname}?${rest.toString()}`);
      }}
    />
  );
}

// =============================================================================
// Billing Panel
// =============================================================================

// The free-tier window at the top of an unsubscribed workspace's Billing page.
// trial_end null (a row the backend has not stamped) reads plain "Free trial".
// ponytail: no "today's allowance used" line; nothing exposes the managed key's
// daily usage yet (see the PR for the backend field it needs).
function TrialStatusCard({
  expired,
  daysLeft,
  endLabel,
}: {
  expired: boolean;
  daysLeft: number | null;
  endLabel: string | null;
}) {
  const title = expired
    ? "This workspace is reading-only"
    : daysLeft == null
      ? "Free trial"
      : `Free trial — ${daysLeft} day${daysLeft === 1 ? "" : "s"} left`;
  return (
    <div className="settings-card" data-testid="trial-status">
      <div className="settings-card-title">{title}</div>
      <div className="settings-card-desc">
        {expired
          ? "Your team's record is all here, and people can still message. Agents start again when an admin picks the plan."
          : `Your agents run on Lightbulb-managed DeepSeek within a daily allowance that resets at midnight UTC.${endLabel ? ` Trial ends ${endLabel}.` : ""}`}
      </div>
    </div>
  );
}

function BillingPanel() {
  // Settings is a direct Checkout success target, so it owns the existing
  // webhook-refresh/URL-cleanup seam as well as the chat shell does.
  useCheckoutReturn();
  const {
    account,
    isLoading,
    error: accountError,
    isSubscribed,
    openPortal,
    startCheckout,
  } = useBilling();
  const { membership } = useOrganization();

  const [portalLoading, setPortalLoading] = useState(false);
  // Mount-time snapshot for the trial-expired check below: render must stay
  // pure (the react-compiler lint rejects Date.now() during render), and the
  // 30-day expiry granularity makes a per-mount timestamp plenty fresh.
  const [nowMs] = useState(() => Date.now());

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

  const subscriptionStatus = account?.subscription_status ?? null;
  const workspacePriceUsd = account?.workspace_price_usd;
  const workspacePlanTitle =
    workspacePriceUsd === undefined ? "Lightbulb plan" : `Lightbulb — $${workspacePriceUsd} / month`;
  const workspaceRecurringPrice =
    workspacePriceUsd === undefined ? "your workspace rate" : `$${workspacePriceUsd}/mo`;
  // No-card free tier. is_subscribed is FALSE for these rows (paid
  // predicate) even though the platform is usable — the card below renders
  // the free-tier framing with a Subscribe CTA instead of Manage Payment
  // (trial_local rows have no Stripe customer, so the portal would fail).
  const isTrialLocal = subscriptionStatus === "trial_local";
  const trialEndLabel =
    account?.trial_end != null
      ? new Date(account.trial_end * 1000).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          year: "numeric",
          // trial_end is a UTC instant; without pinning the zone an owner west
          // of UTC sees it a day early (host-local render). Pinned like the
          // other date sites (Money's money.ts, AgentSpendSection).
          timeZone: "UTC",
        })
      : null;
  // trial_local's `trial_end` is the OVERALL free-tier window expiry — stamped
  // locally at signup as created + 30 days (backend trial_cap._is_expired), NOT
  // the daily allowance reset boundary (which resets at UTC midnight and is
  // tracked separately). Once it elapses the backend blocks usage outright
  // (trial_block_reason -> TRIAL_EXPIRED) WITHOUT flipping subscription_status,
  // so isTrialLocal stays true. Detect the elapsed window here so the card
  // stops promising "a daily allowance of included usage" and instead shows
  // the paused state — while keeping the Subscribe CTA as the owner's path
  // forward.
  const isTrialExpired =
    isTrialLocal &&
    account?.trial_end != null &&
    account.trial_end * 1000 < nowMs;
  // Display mapping for the status chip — the raw enum must never render
  // ("trial_local" would capitalize to "Trial_local"). Other statuses keep
  // the capitalize treatment. Shared with the two chat shells' plan-badge via
  // @/lib/planLabel so the mapping can't drift between surfaces.
  const statusChipLabel = planLabel(subscriptionStatus, { expired: isTrialExpired });

  // Workspace billing is admin-only. Members do not see subscription controls
  // or workspace spend; an unsubscribed admin gets the shared workspace card.
  const isOrgContext = Boolean(membership);
  const orgRole: "admin" | "member" = membership?.role === "org:admin" ? "admin" : "member";

  if (isOrgContext && orgRole === "member") {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>
        <div className="settings-card">
          <div className="settings-card-title">Billing restricted</div>
          <div className="settings-card-desc" style={{ marginTop: 4 }}>
            Contact your organization admin to manage billing and subscription settings.
          </div>
        </div>
      </div>
    );
  }

  // Below this point, isOrgContext (when true) implies orgRole === "admin" —
  // the member case returned above.

  // Billed through Apple's App Store: no Stripe customer exists for this
  // workspace, so the portal would be a dead end and a web checkout would
  // start a second subscription (backend 409s it with "apple_managed" —
  // routers/billing.py). Show the plan and where to actually manage it
  // instead of any Stripe control. past_due (Apple billing retry) counts:
  // it is still Apple's subscription to fix, not a reason to offer Stripe.
  if (
    account?.subscription_source === "apple_iap" &&
    ["active", "trialing", "past_due"].includes(account?.subscription_status ?? "")
  ) {
    const applePlanName =
      account?.plan === "base" ? "Base" : account?.plan === "ultra_pro" ? "Ultra" : "Team";
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">
          {isOrgContext ? "Manage your team's subscription." : "Manage your subscription."}
        </p>
        {accountError && (
          <div className="settings-alert settings-alert-error">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Failed to load billing data. Please refresh the page.
          </div>
        )}
        <div className="settings-card">
          <div className="settings-card-title">Lightbulb — {applePlanName}</div>
          <div className="settings-card-desc">
            Billed through the App Store. Change or cancel on your iPhone: Settings → Apple Account → Subscriptions.
          </div>
        </div>
      </div>
    );
  }

  if (isOrgContext && !isSubscribed) {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>

        {accountError && (
          <div className="settings-alert settings-alert-error">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Failed to load billing data. Please refresh the page.
          </div>
        )}

        {isTrialLocal && (
          <TrialStatusCard
            expired={isTrialExpired}
            daysLeft={freeTierDaysLeft(subscriptionStatus, account?.trial_end, nowMs)}
            endLabel={trialEndLabel}
          />
        )}
        <PlanChooser account={account} subscribed={false} startCheckout={startCheckout} />
      </div>
    );
  }

  if (isOrgContext) {
    // A subscribed workspace uses the normal plan card, with no membership
    // pricing math.
    // orgRole is guaranteed "admin" here (member case returned above).
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>

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
                Lightbulb plan
                {statusChipLabel && (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 8, fontSize: 11, fontWeight: 600, color: "var(--midnight)", background: "color-mix(in srgb, var(--midnight) 8%, transparent)", padding: "2px 8px", borderRadius: 999, textTransform: "capitalize" }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    {statusChipLabel}
                  </span>
                )}
              </div>
              <div className="settings-card-desc">Active subscription</div>
            </div>
            <button type="button" className="settings-btn-outline" onClick={handlePortal} disabled={portalLoading}>
              {portalLoading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Manage Payment
            </button>
          </div>
          <div style={{ marginTop: 14 }}>
            <PlanChooser account={account} subscribed startCheckout={startCheckout} />
          </div>
        </div>
      </div>
    );
  }

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
              {isTrialLocal ? "Lightbulb — Free tier" : workspacePlanTitle}
              {statusChipLabel && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 8, fontSize: 11, fontWeight: 600, color: "var(--midnight)", background: "color-mix(in srgb, var(--midnight) 8%, transparent)", padding: "2px 8px", borderRadius: 999, textTransform: isTrialLocal ? undefined : "capitalize" }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  {statusChipLabel}
                </span>
              )}
            </div>
            {account?.front_desk && (
              <div className="settings-card-desc">
                Front desk{typeof account.front_desk_price_usd === "number" ? ` · $${account.front_desk_price_usd}/month` : ""}
              </div>
            )}
            <div className="settings-card-desc">
              {subscriptionStatus === "trialing"
                ? trialEndLabel
                  ? `Free trial — full access until ${trialEndLabel}, then ${workspaceRecurringPrice}. Cancel anytime before then.`
                  : `Free trial — full access now, then ${workspaceRecurringPrice}. Cancel anytime.`
                : isTrialLocal
                  ? isTrialExpired
                    ? `Free tier ended${trialEndLabel ? ` on ${trialEndLabel}` : ""} — your agents are paused.`
                    : "Free tier — a daily allowance of included usage."
                  : isSubscribed
                    ? "Active subscription"
                    : "No active subscription"}
            </div>
          </div>
          {isSubscribed ? (
            <button type="button" className="settings-btn-outline" onClick={handlePortal} disabled={portalLoading}>
              {portalLoading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Manage Payment
            </button>
          ) : (
            <Link href="/chat" className="settings-btn-save">Sign up</Link>
          )}
        </div>
        {(isSubscribed || isTrialLocal) && (
          <div style={{ marginTop: 14 }}>
            <PlanChooser account={account} subscribed={isSubscribed} startCheckout={startCheckout} />
          </div>
        )}
      </div>
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
    return <div className="text-ui text-muted-foreground">Loading your profile…</div>;
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
  workspace: WorkspacePanel,
  members: MembersPanel,
  connections: ConnectionsPanel,
  billing: BillingPanel,
  usage: UsagePanel,
  budgets: BudgetsPanel,
  providers: ModelPanel,
  mcp: IntegrationsPanel,
  desktop: DesktopSettings,
  // Desktop-app-only: MCP servers running on the user's own Mac, driven by the
  // Tauri local_mcp_* commands. Renders a download prompt in a browser.
  "local-mcp": LocalMcpServers,
  // SkillsTab takes an optional `agentId`; mounting it prop-less yields the
  // owner-level (global) skills surface — the engine-room home.
  skills: () => <SkillsTab />,
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
