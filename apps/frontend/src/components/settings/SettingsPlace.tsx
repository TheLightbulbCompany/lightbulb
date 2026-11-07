"use client";

// The Settings panels, shared by /settings (without the super-agent) and the Settings place in the shell (with it).

import "@/app/settings/settings.css";
import "./settings-place.css";
import React, { useState, useCallback } from "react";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { freeTierDaysLeft, planLabel } from "@/lib/planLabel";
import { useAuth, useUser, useOrganization, useClerk } from "@clerk/nextjs";
import { useApi, ApiError, BACKEND_URL } from "@/lib/api";
import { useBilling } from "@/hooks/useBilling";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { WorkspaceMembersSection } from "@/components/settings/WorkspaceMembersSection";
import { DailySummaryCard } from "@/components/settings/DailySummaryCard";
import { PlanChooser } from "@/components/settings/PlanChooser";
import { UsagePanel } from "@/components/control/panels/UsagePanel";
import { ConnectionsPanel } from "@/components/control/panels/ConnectionsPanel";
import { ApprovalsPanel } from "@/components/settings/AgentGuardrails";
import { SettingsPanel as CompanySettingsPanel } from "@/components/teams/panels/SettingsPanel";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  ADMIN_ONLY_PANELS,
  SETTINGS_NAV_SECTIONS,
  type NavItem,
} from "@/components/settings/settings-nav-config";
import { Button } from "@/components/ui/button";
import { SettingRow } from "@/components/settings/SettingRow";
import { MoneyPanel } from "@/components/teams/panels/MoneyPanel";
import { replaceSearchParams } from "@/lib/shellNav";
import { openPanel } from "@/components/workspace/workspaceController";
import type { PanelProps } from "@/components/workspace/panelComponents";

// =============================================================================
// Types & Navigation
//
// The nav sections, the NavItem type, and NavIcon live in the shared
// settings-nav-config module. This page re-imports the section consts +
// NavItem above; <SettingsNav> imports the same module.
// =============================================================================

export type Panel =
  | "profile"
  | "workspace"
  | "members"
  | "approvals"
  | "connections"
  | "billing"
  | "usage"
  | "business";

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

  if (!open) return (
    <SettingRow label="Delete account" line="Removes your account for good. As the only member, the workspace goes too.">
      <Button type="button" size="sm" variant="outline" className="text-err" onClick={() => setOpen(true)}>Delete…</Button>
    </SettingRow>
  );
  return (
    <div className="settings-card" style={{ borderColor: "color-mix(in srgb, var(--err) 25%, transparent)", marginTop: 16 }}>
      <div className="settings-card-title" style={{ color: DANGER }}>Delete account</div>
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
      const response = await fetch(`${BACKEND_URL}/tasks/export`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      // Only a 200 carrying NDJSON is a finished export; an error body is JSON.
      const contentType = response.headers.get("content-type") ?? "";
      if (response.status !== 200 || !contentType.includes("application/x-ndjson")) {
        throw new Error(`Export failed (${response.status})`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") ?? "";
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? "lightbulb-tasks.ndjson";
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
    <div data-testid="workspace-export-card">
      <SettingRow label="Export workspace" line="A file of every task in this workspace and what it made. Works even when the workspace is read-only.">
        <Button type="button" size="sm" variant="outline" onClick={handleExport} disabled={busy}>{busy ? "Preparing…" : "Download"}</Button>
      </SettingRow>
      {error && <p role="alert" className="settings-rows__line text-err pt-2">{error}</p>}
    </div>
  );
}

/** Profile (W13-10): who you are with Edit profile beside it, then one row per setting, no card each. */
function ProfilePanel() {
  const { user } = useUser();
  const { openUserProfile } = useClerk();
  const { account } = useBilling();
  const plan = planLabel(account?.subscription_status ?? null);
  const initials = `${(user?.firstName ?? "")[0] ?? ""}${(user?.lastName ?? "")[0] ?? ""}`.toUpperCase();
  return (
    <div className="settings-panel" id="panel-profile" role="tabpanel">
      <h1 className="settings-page-title">Profile</h1>
      <div className="settings-rows">
        <div className="settings-rows__row">
          <div className="settings-profile-avatar-lg settings-profile-avatar--row" aria-hidden="true">{initials}</div>
          <div className="settings-rows__text">
            <p className="settings-rows__label">{user?.fullName ?? "You"}{plan && <span className="settings-rows__line"> · {plan}</span>}</p>
            <p className="settings-rows__line">{user?.primaryEmailAddress?.emailAddress ?? ""}</p>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={() => openUserProfile?.()}>Edit profile</Button>
        </div>
        <DailySummaryCard rows />
        <WorkspaceExportCard />
        <DeleteAccountCard />
      </div>
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
// Panel Registry
// =============================================================================

export const PANELS: Record<Panel, () => React.ReactElement> = {
  profile: ProfilePanel,
  workspace: WorkspacePanel,
  members: MembersPanel,
  approvals: ApprovalsPanel,
  connections: ConnectionsPanel,
  billing: BillingPanel,
  usage: UsagePanel,
  business: BusinessPanel,
};

// =============================================================================
// Business (super-agent): what Work › Money showed, inside Settings › Plan.
// =============================================================================

function BusinessPanel() {
  return (
    <div className="settings-panel" id="panel-business" role="tabpanel">
      <h1 className="settings-page-title">Business</h1>
      <p className="settings-page-desc">Revenue, customers and what you sell, from Stripe, and what your agents spent.</p>
      <MoneyPanel embedded />
    </div>
  );
}

// =============================================================================
// Settings as a place in the shell (super-agent, UX slice 4): the rail's ⚙ opens it beside the sidebar, Back is the
// browser's, and `section` names the nav row. /settings redirects here.
// =============================================================================

/** Arrow keys move between the in-page rows (roving focus); Enter and Space pick the focused one. */
export function navKeyDown(e: React.KeyboardEvent, item: NavItem & { panel: string }, rows: readonly (NavItem & { panel: string })[], select: (panel: string) => void): void {
  const idx = rows.findIndex((n) => n.id === item.id);
  const step = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    select(item.panel);
  } else if (step) {
    e.preventDefault();
    const next = rows[(idx + step + rows.length) % rows.length];
    select(next.panel);
    document.getElementById(`settings-nav-${next.id}`)?.focus();
  }
}

export function SettingsPlace({ params }: PanelProps) {
  const sections = SETTINGS_NAV_SECTIONS;
  const isAdmin = useIsWorkspaceAdmin();
  const visible = (item: NavItem): item is NavItem & { panel: Panel } =>
    item.panel !== undefined && Object.hasOwn(PANELS, item.panel) && (isAdmin || !ADMIN_ONLY_PANELS.has(item.panel));
  const rows = sections.flatMap((s) => s.items).filter(visible);
  // Derived, not latched: isAdmin reads false until Clerk knows the membership, so a deep-linked admin row
  // re-resolves once it does.
  const active: Panel = rows.find((row) => row.panel === params.section)?.panel ?? "profile";
  // A link that names a row this person can't open here says why, in one line, above Profile.
  const named = params.section && active !== params.section
    ? SETTINGS_NAV_SECTIONS.flatMap((s) => s.items).find((item) => item.panel === params.section) : undefined;
  const why = !named ? null
    : ADMIN_ONLY_PANELS.has(named.panel!) && !isAdmin ? `${named.name} is for workspace admins. Ask one to open it.`
      : `${named.name} isn't part of Settings any more.`;
  const select = (section: string) => {
    openPanel("settings", { section });
    // Moving between rows is not a new place: Back leaves Settings rather than walking its rows.
    replaceSearchParams(new URLSearchParams({ panel: "settings", section }));
  };
  const Active = PANELS[active];
  return (
    <div className="settings-place">
      <div className="frame-doc">
        <div className="frame-doc__title"><h1>Settings</h1></div>
        <div className="settings-place__layout">
          <label className="settings-place__picker">
            <span className="sr-only">Settings section</span>
            <select value={active} onChange={(e) => select(e.target.value)}>
              {sections.map((s) => <optgroup key={s.label} label={s.label}>
                {s.items.filter(visible).map((item) => <option key={item.id} value={item.panel}>{item.name}</option>)}
              </optgroup>)}
            </select>
          </label>
          <SettingsNav mode="tabs" sections={sections} isAdmin={isAdmin} activePanel={active} onSelectPanel={select}
            onPanelKeyDown={(e, item) => navKeyDown(e, item, rows, select)} />
          <section className="settings-place__content" aria-label={rows.find((r) => r.panel === active)?.name ?? "Settings"}>
            {why && <p role="status" className="settings-place__why">{why}</p>}
            <Active key={active} />
          </section>
        </div>
      </div>
    </div>
  );
}
